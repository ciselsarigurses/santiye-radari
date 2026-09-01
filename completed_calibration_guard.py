"""Saha etiketi tamamlanan alarm-dışı kalibrasyon noktasını tekrar seçtirmez.

Kalibrasyon sayfası daha önce kaydedilmiş bir noktayı arayüzde gizler; ancak rota
seçiciler bu kaydı bilmiyorsa aynı nokta ``latest_report.json`` içinde günlük
kalibrasyon slotunu işgal etmeye devam edebilir. Bu koruma yalnız tamamlanmış saha
geri bildirimini okur ve güvenli bir alternatif varsa aynı bölgeden sonraki uygun
kuru-zemin adayına geçer.

Yeni alarm/görev üretmez; Sentinel eşiği, 250 m² alt sınırı, aktif görev mesafesi ve
günlük kalibrasyon üst sınırı değişmez. Alternatif yoksa tamamlanmış nokta rapordan
çıkarılır; doğrulanmış sonucu silinmez ve normal saha istatistiğine eklenmez.
"""

from __future__ import annotations

import argparse
import json

import calibration_outcome
import calibration_rotation_guard as calibration
import temporal_calibration_rotation_guard as temporal_rotation


NOTE_SUFFIX = (
    " Tamamlanmış saha etiketi olan kalibrasyon noktası tekrar günlük slota alınmaz; "
    "aynı bölgede tüm güvenlik filtrelerini geçen başka aday varsa sonraki güvenli "
    "aday seçilir, yoksa slot boş bırakılır."
)


def _recorded(item, recorded):
    if not isinstance(item, dict):
        return False
    return any(
        key in recorded
        for key in calibration_outcome.calibration_id_aliases(item)
    )


def _decorate_ranked(region_key, region_data, report, audit, temporal_payload, locality_payload):
    active = calibration.route._actionable_candidates(report.get("saha_adaylari") or [])
    ranked = calibration._eligible_region_items(region_key, region_data, active)
    if not ranked:
        return [], 0

    report_date = report.get("rapor_tarihi") or audit.get("rapor_tarihi")
    temporal_map = calibration._temporal_region_map(
        temporal_payload,
        region_key,
        region_data,
        report_date,
    )
    ranked = calibration._attach_temporal_evidence(ranked, temporal_map)
    locality_map = temporal_rotation._locality_region_map(
        locality_payload,
        region_key,
        region_data,
        report_date,
    )
    ranked = temporal_rotation._attach_locality_evidence(ranked, locality_map)
    age_days = calibration._days_since_scene(report_date, region_data.get("son_tarih"))
    return temporal_rotation._ordered_candidates(ranked, age_days), age_days


def _replacement_for_region(
    region_key,
    selected,
    recorded,
    report,
    audit,
    temporal_payload,
    locality_payload,
):
    regions = audit.get("bolgeler") or {}
    region_data = regions.get(region_key) if isinstance(regions, dict) else None
    if not isinstance(region_data, dict):
        return None

    ordered, age_days = _decorate_ranked(
        region_key,
        region_data,
        report,
        audit,
        temporal_payload,
        locality_payload,
    )
    if not ordered:
        return None

    for raw in ordered:
        if _recorded(raw, recorded):
            continue
        if not calibration.route._far_from_selected(raw, selected):
            continue
        chosen = dict(raw)
        chosen["kalibrasyon_rotasyon_gun"] = int(age_days)
        chosen["kalibrasyon_rotasyon_havuzu"] = min(
            len(ordered), calibration.ROTATION_POOL_PER_REGION
        )
        if temporal_rotation._temporal_urgent(chosen):
            if bool(chosen.get("yerellik_lokal_ani_baslangic_destegi")):
                chosen["kalibrasyon_nedeni"] = "ZAMAN_SERISI_LOKAL_ANI_BASLANGIC_ROTASYON"
            else:
                chosen["kalibrasyon_nedeni"] = "ZAMAN_SERISI_ANI_BASLANGIC_ROTASYON"
        return chosen
    return None


def select_unrecorded_calibration(
    report,
    audit,
    temporal_payload,
    locality_payload,
    recorded,
):
    current = [
        dict(item)
        for item in report.get("kuru_zemin_kalibrasyon_kontrolu") or []
        if isinstance(item, dict)
    ]
    if not current or not recorded:
        return current

    selected = []
    for item in current:
        if not _recorded(item, recorded):
            if calibration.route._far_from_selected(item, selected):
                selected.append(dict(item))
            continue

        region_key = str(item.get("bolge_anahtari") or "")
        replacement = _replacement_for_region(
            region_key,
            selected,
            recorded,
            report,
            audit,
            temporal_payload,
            locality_payload,
        )
        if replacement is not None:
            selected.append(replacement)

    assert len(selected) <= len(current), "Tamamlanan kalibrasyon koruması slot sayısını artırdı."
    assert all(
        item.get("kalibrasyon_durumu") == "ALARM_DEGIL" for item in selected
    ), "Kalibrasyon alarm-dışı statüsü bozuldu."
    assert not any(_recorded(item, recorded) for item in selected), (
        "Tamamlanmış kalibrasyon noktası günlük rotasyonda kaldı."
    )
    return selected


def update_completed_guard(recorded=None):
    audit = temporal_rotation._load_json(calibration.DRY_GROUND_AUDIT)
    report = temporal_rotation._load_json(calibration.REPORT_JSON)
    temporal_payload = temporal_rotation._load_json(calibration.DRY_GROUND_TEMPORAL_AUDIT)
    locality_payload = temporal_rotation._load_json(temporal_rotation.LOCALITY_AUDIT)
    if audit is None or report is None:
        return []

    recorded = recorded if recorded is not None else calibration_outcome.calibration_outcome_map()
    selected = select_unrecorded_calibration(
        report,
        audit,
        temporal_payload,
        locality_payload,
        recorded,
    )
    report["kuru_zemin_kalibrasyon_kontrolu"] = selected
    note = str(report.get("kuru_zemin_kalibrasyon_notu") or "").strip()
    if NOTE_SUFFIX.strip() not in note:
        report["kuru_zemin_kalibrasyon_notu"] = (note + NOTE_SUFFIX).strip()
    calibration.REPORT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if calibration.FIELD_REPORT_MD.exists():
        current = calibration.FIELD_REPORT_MD.read_text(encoding="utf-8")
        calibration.FIELD_REPORT_MD.write_text(
            calibration.route._inject_calibration_markdown(
                current,
                calibration.route._calibration_markdown(selected),
            ),
            encoding="utf-8",
        )
    return selected


def _self_check():
    west = calibration.route.SATELLITE_REGION_LABELS[0]

    def candidate(name, lat, lon, bsi):
        return {
            "mahalle": name,
            "enlem": lat,
            "boylam": lon,
            "alan_m2": 400,
            "ortalama_bsi_degisim": bsi,
            "ortalama_rgb_farki": 0.18,
            "kompaktlik": 0.60,
            "saha_benzeri_geometri": True,
            "izole_saha_benzeri": True,
            "lineer_geometri_riski": False,
            "bolge_anahtari": "cesme",
            "bolge": west,
            "onceki_tarih": "26.08.2026",
            "son_tarih": "29.08.2026",
            "kalibrasyon_durumu": "ALARM_DEGIL",
        }

    first = candidate("W1", 38.20, 26.30, 0.40)
    second = candidate("W2", 38.22, 26.32, 0.35)
    third = candidate("W3", 38.24, 26.34, 0.30)
    audit = {
        "rapor_tarihi": "2026-09-01",
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "bolge": west,
                "onceki_tarih": "26.08.2026",
                "son_tarih": "29.08.2026",
                "saha_benzeri_ornekler": [first, second, third],
            }
        },
    }
    report = {
        "rapor_tarihi": "2026-09-01",
        "saha_adaylari": [],
        "kuru_zemin_kalibrasyon_kontrolu": [first],
    }
    recorded = {calibration_outcome.calibration_id(first): {"sonuc": "SANTIYE_KAZI"}}
    chosen = select_unrecorded_calibration(report, audit, None, None, recorded)
    assert len(chosen) == 1, chosen
    assert chosen[0]["mahalle"] == "W2", chosen
    assert not _recorded(chosen[0], recorded)

    all_recorded = {
        calibration_outcome.calibration_id(first): {"sonuc": "SANTIYE_KAZI"},
        calibration_outcome.calibration_id(second): {"sonuc": "TARLA_BITKI"},
        calibration_outcome.calibration_id(third): {"sonuc": "YANLIS_POZITIF"},
    }
    none_left = select_unrecorded_calibration(report, audit, None, None, all_recorded)
    assert none_left == [], none_left

    unchanged = select_unrecorded_calibration(report, audit, None, None, {})
    assert unchanged == [first], unchanged


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Tamamlanan kalibrasyon rotasyon öz testi başarılı.")
        return
    chosen = update_completed_guard()
    print(
        "Tamamlanan kalibrasyon noktaları günlük rotasyondan çıkarıldı: "
        + (", ".join(str(item.get("mahalle") or "?") for item in chosen) or "açık kalibrasyon yok")
    )


if __name__ == "__main__":
    main()
