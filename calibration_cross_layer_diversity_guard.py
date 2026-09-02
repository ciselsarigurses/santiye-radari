"""Alarm-dışı kalibrasyonu günlük saha katmanlarıyla aynı mahalleye yığılmaktan korur.

Ana saha görevleri, kuru-zemin kalibrasyonu ve Sentinel kör-alan devriyesi ayrı amaçlara
hizmet eder. Ancak iki örtüşen Sentinel kutusu aynı mahalleleri görebildiği için aynı gün
ilk üç saha görevi + iki kalibrasyon + iki kör-alan noktasının birkaç mahallede yığılması
mümkündür. Bu katman ana görevleri veya kör-alan devriyesini değiştirmez; yalnız alarm-dışı
kalibrasyon noktasını, temporal/yerellik kalite sınıfını düşürmeden güvenli bir alternatif
varsa farklı mahalleye taşır.

Yeni alarm/görev üretmez; 250 m² alt sınırı, aktif görev mesafesi, Sentinel eşikleri ve
kalibrasyon slot sayısı artırılmaz. Eş kalite alternatifi yoksa mevcut güçlü nokta korunur.
Saha etiketi tamamlanmış bir nokta sonraki post-process adımlarında yanlışlıkla geri
seçilmişse tekrar kullanılmaz.
"""

from __future__ import annotations

import argparse
import json

import calibration_outcome
import calibration_rotation_guard as calibration
import completed_calibration_guard as completed
import coverage_patrol_shortlist as coverage
import temporal_calibration_rotation_guard as temporal_rotation


NOTE_SUFFIX = (
    " Ana ilk-3 saha rotası veya nihai kör-alan devriyesiyle aynı mahallede kalan "
    "alarm-dışı kalibrasyon, temporal/yerellik kalite sınıfını düşürmeden güvenli "
    "alternatif varsa farklı mahalleye taşınır; eş kalite alternatif yoksa güçlü "
    "mevcut nokta korunur."
)


def _neighborhood(value):
    return coverage._neighborhood_key(value)


def _blocked_neighborhoods(report_payload):
    blocked = set()
    for field in ("gunun_ilk_3_kontrolu", "kor_alan_saha_devriyesi"):
        for item in (report_payload or {}).get(field) or []:
            if not isinstance(item, dict):
                continue
            key = _neighborhood(item.get("mahalle"))
            if key:
                blocked.add(key)
    return blocked


def _evidence_class(item):
    """Düşük sayı daha güçlü temporal/yerellik kanıt sınıfıdır."""
    if temporal_rotation._temporal_urgent(item):
        if bool(item.get("yerellik_lokal_ani_baslangic_destegi")):
            return 0
        return 1
    return 2


def _decorate_choice(item, age_days, ordered):
    chosen = dict(item)
    chosen["kalibrasyon_rotasyon_gun"] = int(age_days)
    chosen["kalibrasyon_rotasyon_havuzu"] = min(
        len(ordered), calibration.ROTATION_POOL_PER_REGION
    )
    if temporal_rotation._temporal_urgent(chosen):
        if bool(chosen.get("yerellik_lokal_ani_baslangic_destegi")):
            chosen["kalibrasyon_nedeni"] = "ZAMAN_SERISI_LOKAL_ANI_BASLANGIC_ROTASYON"
        else:
            chosen["kalibrasyon_nedeni"] = "ZAMAN_SERISI_ANI_BASLANGIC_ROTASYON"
    chosen["katmanlar_arasi_mahalle_cesitliligi"] = True
    return chosen


def select_cross_layer_diverse_calibration(
    report_payload,
    audit_payload,
    temporal_payload,
    locality_payload,
    recorded=None,
):
    current = [
        dict(item)
        for item in (report_payload or {}).get("kuru_zemin_kalibrasyon_kontrolu") or []
        if isinstance(item, dict)
    ]
    if not current:
        return []

    regions = (audit_payload or {}).get("bolgeler") or {}
    if not isinstance(regions, dict):
        return current

    recorded = (
        recorded
        if recorded is not None
        else calibration_outcome.calibration_outcome_map()
    )
    blocked = _blocked_neighborhoods(report_payload)
    selected = []
    selected_neighborhoods = set()

    for original in current:
        original_key = _neighborhood(original.get("mahalle"))
        was_recorded = completed._recorded(original, recorded)
        conflict = bool(
            original_key
            and (original_key in blocked or original_key in selected_neighborhoods)
        )

        if not was_recorded and not conflict:
            selected.append(dict(original))
            if original_key:
                selected_neighborhoods.add(original_key)
            continue

        region_key = str(original.get("bolge_anahtari") or "")
        region_data = regions.get(region_key)
        ordered, age_days = completed._decorate_ranked(
            region_key,
            region_data,
            report_payload,
            audit_payload,
            temporal_payload,
            locality_payload,
        )
        original_class = _evidence_class(original)
        replacement = None

        for candidate in ordered:
            if completed._recorded(candidate, recorded):
                continue
            candidate_key = _neighborhood(candidate.get("mahalle"))
            if candidate_key and (
                candidate_key in blocked or candidate_key in selected_neighborhoods
            ):
                continue
            if not calibration.route._far_from_selected(candidate, selected):
                continue
            # Sırf mahalle çeşitliliği için güçlü temporal/yerellik kanıtını düşürme.
            # Tamamlanmış bir nokta geri geldiyse ise aynı slotu kurtarmak için herhangi
            # bir mevcut güvenli, kaydedilmemiş alternatif kabul edilebilir.
            if not was_recorded and _evidence_class(candidate) > original_class:
                continue
            replacement = _decorate_choice(candidate, age_days, ordered)
            break

        if replacement is not None:
            selected.append(replacement)
            key = _neighborhood(replacement.get("mahalle"))
            if key:
                selected_neighborhoods.add(key)
            continue

        # Eş kalite alternatif yoksa gerçek güçlü noktayı sırf çeşitlilik adına atma.
        # Ancak saha sonucu tamamlanmış bir nokta tekrar kalibrasyona dönmemeli.
        if not was_recorded:
            selected.append(dict(original))
            if original_key:
                selected_neighborhoods.add(original_key)

    assert len(selected) <= len(current), (
        "Katmanlar arası çeşitlilik kalibrasyon slot sayısını artırdı."
    )
    assert all(
        item.get("kalibrasyon_durumu") == "ALARM_DEGIL" for item in selected
    ), "Katmanlar arası çeşitlilik alarm-dışı statüyü bozdu."
    assert not any(completed._recorded(item, recorded) for item in selected), (
        "Tamamlanmış saha etiketi post-process sonrası kalibrasyona geri döndü."
    )
    return selected


def update_cross_layer_diversity(recorded=None):
    report = temporal_rotation._load_json(calibration.REPORT_JSON)
    audit = temporal_rotation._load_json(calibration.DRY_GROUND_AUDIT)
    temporal_payload = temporal_rotation._load_json(calibration.DRY_GROUND_TEMPORAL_AUDIT)
    locality_payload = temporal_rotation._load_json(temporal_rotation.LOCALITY_AUDIT)
    if report is None or audit is None:
        return []

    selected = select_cross_layer_diverse_calibration(
        report,
        audit,
        temporal_payload,
        locality_payload,
        recorded=recorded,
    )
    report["kuru_zemin_kalibrasyon_kontrolu"] = selected
    report["kalibrasyon_katmanlar_arasi_cesitlilik"] = {
        "durum": "uygulandi",
        "engellenen_mahalleler": sorted(_blocked_neighborhoods(report)),
        "secilen_mahalleler": [
            str(item.get("mahalle") or "") for item in selected
        ],
        "kural": (
            "Ana ilk-3 veya kor-alan devriyesi mahallesini, ayni temporal/yerellik "
            "kalite sinifinda guvenli alternatif varsa alarm-disi kalibrasyonda tekrar etme."
        ),
    }
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
                calibration._rotation_markdown(selected),
            ),
            encoding="utf-8",
        )
    return selected


def _self_check():
    west = calibration.route.SATELLITE_REGION_LABELS[0]
    east = calibration.route.SATELLITE_REGION_LABELS[1]

    def candidate(name, lat, lon, region_key):
        return {
            "mahalle": name,
            "enlem": lat,
            "boylam": lon,
            "alan_m2": 400,
            "ortalama_bsi_degisim": 0.16,
            "ortalama_rgb_farki": 0.17,
            "kompaktlik": 0.70,
            "saha_benzeri_geometri": True,
            "izole_saha_benzeri": True,
            "lineer_geometri_riski": False,
            "bolge_anahtari": region_key,
            "bolge": west if region_key == "cesme" else east,
            "onceki_tarih": "26.08.2026",
            "son_tarih": "29.08.2026",
            "kalibrasyon_durumu": "ALARM_DEGIL",
            "zaman_serisi_ani_baslangic_destegi": True,
            "zaman_serisi_ani_baslangic_orani": 8.0,
            "zaman_serisi_istikrarsiz_zemin_riski": False,
            "yerellik_lokal_ani_baslangic_destegi": True,
            "yerellik_yaygin_cevre_degisim_riski": False,
            "yerellik_orani": 1.7,
        }

    west_alacati = candidate("Alaçatı", 38.25, 26.41, "cesme")
    east_alacati = candidate("Alaçatı", 38.21, 26.46, "uzunkuyu")
    east_germiyan = candidate("Germiyan", 38.33, 26.48, "uzunkuyu")

    audit = {
        "rapor_tarihi": "2026-09-02",
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "bolge": west,
                "onceki_item": "OLD",
                "son_item": "NEW",
                "onceki_tarih": "26.08.2026",
                "son_tarih": "29.08.2026",
                "saha_benzeri_ornekler": [west_alacati],
            },
            "uzunkuyu": {
                "durum": "ok",
                "bolge": east,
                "onceki_item": "OLD",
                "son_item": "NEW",
                "onceki_tarih": "26.08.2026",
                "son_tarih": "29.08.2026",
                "saha_benzeri_ornekler": [east_alacati, east_germiyan],
            },
        },
    }
    report = {
        "rapor_tarihi": "2026-09-02",
        "saha_adaylari": [],
        "gunun_ilk_3_kontrolu": [{"mahalle": "Ildır"}],
        "kor_alan_saha_devriyesi": [{"mahalle": "Uzunkuyu"}],
        "kuru_zemin_kalibrasyon_kontrolu": [west_alacati, east_alacati],
    }

    def temporal_row(item, ratio):
        return {
            **item,
            "ani_baslangic_destegi": True,
            "ani_baslangic_orani": ratio,
            "onceki_donem_bsi_degisim": 0.02,
            "onceki_donem_gecerli_oran": 1.0,
            "istikrarsiz_zemin_riski": False,
        }

    temporal_payload = {
        "rapor_tarihi": "2026-09-02",
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "onceki_tarih": "26.08.2026",
                "son_tarih": "29.08.2026",
                "adaylar": [temporal_row(west_alacati, 9.0)],
            },
            "uzunkuyu": {
                "durum": "ok",
                "onceki_tarih": "26.08.2026",
                "son_tarih": "29.08.2026",
                "adaylar": [
                    temporal_row(east_alacati, 8.0),
                    temporal_row(east_germiyan, 7.0),
                ],
            },
        },
    }
    locality_payload = {
        "rapor_tarihi": "2026-09-02",
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "onceki_item": "OLD",
                "son_item": "NEW",
                "adaylar": [
                    {**west_alacati, "lokal_ani_baslangic_destegi": True,
                     "yaygin_cevre_degisim_riski": False, "yerellik_orani": 1.8}
                ],
            },
            "uzunkuyu": {
                "durum": "ok",
                "onceki_item": "OLD",
                "son_item": "NEW",
                "adaylar": [
                    {**east_alacati, "lokal_ani_baslangic_destegi": True,
                     "yaygin_cevre_degisim_riski": False, "yerellik_orani": 1.7},
                    {**east_germiyan, "lokal_ani_baslangic_destegi": True,
                     "yaygin_cevre_degisim_riski": False, "yerellik_orani": 1.6},
                ],
            },
        },
    }

    selected = select_cross_layer_diverse_calibration(
        report, audit, temporal_payload, locality_payload, recorded={}
    )
    assert [item["mahalle"] for item in selected] == ["Alaçatı", "Germiyan"], selected
    assert all(_evidence_class(item) == 0 for item in selected), selected

    # Ana rotada Alaçatı zaten varsa, eş kalite alternatif yokken güçlü kalibrasyon
    # sırf çeşitlilik için kaybolmamalı.
    blocked_report = json.loads(json.dumps(report))
    blocked_report["gunun_ilk_3_kontrolu"] = [{"mahalle": "Alaçatı"}]
    west_only = json.loads(json.dumps(audit))
    west_only["bolgeler"]["uzunkuyu"]["saha_benzeri_ornekler"] = [east_germiyan]
    preserved = select_cross_layer_diverse_calibration(
        blocked_report, west_only, temporal_payload, locality_payload, recorded={}
    )
    assert preserved, preserved
    assert all(item.get("kalibrasyon_durumu") == "ALARM_DEGIL" for item in preserved)

    print(
        "Katmanlar arası kalibrasyon çeşitliliği öz testi başarılı; eş kalite temporal/yerellik "
        "kanıtı korunarak mahalle tekrarı azaltılıyor."
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        return 0
    chosen = update_cross_layer_diversity()
    print(
        "Katmanlar arası alarm-dışı kalibrasyon çeşitliliği güncellendi: "
        + (", ".join(str(item.get("mahalle") or "?") for item in chosen) or "kalibrasyon yok")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
