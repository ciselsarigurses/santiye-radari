"""Son kör-alan rotasından sonra kuru-zemin kalibrasyonunda mahalle tekrarını azaltır.

Kör-alan rotasyonu çekirdek/ortak-şerit dengesini korumak için en son çalışır. Bu doğru
sıralama bazı günlerde kuru-zemin kalibrasyonu ile nihai kör-alan devriyesinin aynı
mahalleyi yeniden seçmesine yol açabilir. Bu son koruma alarm veya görev üretmez ve
nokta sayısını artırmaz. Yalnız mevcut kalibrasyon mahallesi nihai kör-alan devriyesinde
de varsa, aynı Sentinel bölgesindeki zaten güvenli ve rotasyonda bulunan başka bir
kuru-zemin örneğine geçer. Güvenli alternatif yoksa mevcut kalibrasyon korunur.

250 m² alt sınırı, aktif görev mesafesi, izole/saha-benzeri geometri ve lineer-risk
filtreleri ``calibration_rotation_guard`` ile aynıdır; burada yeni eşik tanımlanmaz.
"""

from __future__ import annotations

import json

import calibration_rotation_guard as calibration
import coverage_patrol_shortlist as coverage


NOTE = (
    "Alarm/görev değildir; üretim maskesinin dışındaki izole, saha-benzeri kuru-zemin "
    "değişimlerinden aktif görevlerin dışında bölge başına en fazla bir, toplam iki "
    "örnek tutulur. Nihai Sentinel kör-alan devriyesi aynı mahalleyi de seçmişse güvenli "
    "bir kuru-zemin alternatifi tercih edilir; alternatif yoksa mevcut kalibrasyon "
    "korunur."
)


def _neighborhood(value):
    return coverage._neighborhood_key(value)


def _coverage_neighborhoods(report_payload):
    values = set()
    for item in (report_payload or {}).get("kor_alan_saha_devriyesi") or []:
        if not isinstance(item, dict):
            continue
        key = _neighborhood(item.get("mahalle"))
        if key:
            values.add(key)
    return values


def _existing_by_region(report_payload):
    values = {}
    for item in (report_payload or {}).get("kuru_zemin_kalibrasyon_kontrolu") or []:
        if not isinstance(item, dict):
            continue
        region_key = str(item.get("bolge_anahtari") or "")
        if region_key and region_key not in values:
            values[region_key] = dict(item)
    return values


def _decorate_rotation(item, report_date, region_data, ranked):
    updated = dict(item)
    age_days = calibration._days_since_scene(
        report_date,
        region_data.get("son_tarih"),
    )
    updated["kalibrasyon_rotasyon_gun"] = int(age_days)
    updated["kalibrasyon_rotasyon_havuzu"] = min(
        len(ranked), calibration.ROTATION_POOL_PER_REGION
    )
    return updated


def select_final_diverse_calibration(audit_payload, report_payload):
    if not isinstance(audit_payload, dict) or not isinstance(report_payload, dict):
        return []

    current = [
        dict(item)
        for item in report_payload.get("kuru_zemin_kalibrasyon_kontrolu") or []
        if isinstance(item, dict)
    ]
    if not current:
        return []

    blocked = _coverage_neighborhoods(report_payload)
    if not blocked:
        return current

    regions = audit_payload.get("bolgeler") or {}
    if not isinstance(regions, dict):
        return current

    existing = _existing_by_region(report_payload)
    active = calibration.route._actionable_candidates(
        report_payload.get("saha_adaylari") or []
    )
    report_date = (
        report_payload.get("rapor_tarihi")
        or audit_payload.get("rapor_tarihi")
    )
    selected = []
    selected_neighborhoods = set()

    # Yalnız mevcut kalibrasyonun olduğu bölgeleri işleriz; bu katman yeni slot açmaz.
    for region_key, original in existing.items():
        region_data = regions.get(region_key)
        original_neighborhood = _neighborhood(original.get("mahalle"))

        # Çakışma yoksa önceki güvenli/rotasyonlu seçimi aynen koru.
        if original_neighborhood not in blocked:
            chosen = dict(original)
        else:
            ranked = calibration._eligible_region_items(
                region_key,
                region_data,
                active,
            )
            age_days = calibration._days_since_scene(
                report_date,
                (region_data or {}).get("son_tarih") if isinstance(region_data, dict) else None,
            )
            ordered = calibration._rotated_order(ranked, age_days)
            alternatives = []
            for candidate in ordered:
                key = _neighborhood(candidate.get("mahalle"))
                if key in blocked or key in selected_neighborhoods:
                    continue
                if not calibration.route._far_from_selected(candidate, selected):
                    continue
                alternatives.append(candidate)

            if alternatives:
                chosen = _decorate_rotation(
                    alternatives[0],
                    report_date,
                    region_data,
                    ranked,
                )
            else:
                # Çekirdek/kör-alan kapsamasını bozmak için kalibrasyonu silme.
                chosen = dict(original)

        selected.append(chosen)
        key = _neighborhood(chosen.get("mahalle"))
        if key:
            selected_neighborhoods.add(key)

    # Orijinal sıra/slot sayısı korunur. Bilinmeyen eski kayıt varsa sona eklenir.
    known_ids = {
        str(item.get("bolge_anahtari") or "")
        for item in selected
    }
    for item in current:
        region_key = str(item.get("bolge_anahtari") or "")
        if region_key and region_key in known_ids:
            continue
        selected.append(dict(item))

    assert len(selected) == len(current), (
        "Son kalibrasyon çeşitliliği kalibrasyon nokta sayısını değiştirdi."
    )
    return selected


def update_final_diversity():
    if not calibration.DRY_GROUND_AUDIT.exists() or not calibration.REPORT_JSON.exists():
        return []

    audit = json.loads(calibration.DRY_GROUND_AUDIT.read_text(encoding="utf-8"))
    report = json.loads(calibration.REPORT_JSON.read_text(encoding="utf-8"))
    selected = select_final_diverse_calibration(audit, report)

    report["kuru_zemin_kalibrasyon_kontrolu"] = selected
    report["kuru_zemin_kalibrasyon_notu"] = NOTE
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
        }

    audit = {
        "rapor_tarihi": "2026-09-01",
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "bolge": "Batı",
                "onceki_tarih": "26.08.2026",
                "son_tarih": "29.08.2026",
                "saha_benzeri_ornekler": [
                    candidate("Reisdere", 38.31, 26.40, 0.40),
                    candidate("Ovacık", 38.25, 26.33, 0.35),
                ],
            },
            "uzunkuyu": {
                "durum": "ok",
                "bolge": "Doğu",
                "onceki_tarih": "26.08.2026",
                "son_tarih": "29.08.2026",
                "saha_benzeri_ornekler": [
                    candidate("Uzunkuyu", 38.28, 26.55, 0.42),
                    candidate("Germiyan", 38.32, 26.50, 0.36),
                ],
            },
        },
    }
    report = {
        "rapor_tarihi": "2026-09-01",
        "saha_adaylari": [],
        "kor_alan_saha_devriyesi": [
            {"mahalle": "Uzunkuyu", "enlem": 38.20, "boylam": 26.58},
            {"mahalle": "Alaçatı", "enlem": 38.24, "boylam": 26.48},
        ],
        "kuru_zemin_kalibrasyon_kontrolu": [
            {
                **candidate("Reisdere", 38.31, 26.40, 0.40),
                "bolge_anahtari": "cesme",
            },
            {
                **candidate("Uzunkuyu", 38.28, 26.55, 0.42),
                "bolge_anahtari": "uzunkuyu",
            },
        ],
    }
    selected = select_final_diverse_calibration(audit, report)
    assert [item["mahalle"] for item in selected] == ["Reisdere", "Germiyan"], selected
    assert len(selected) == 2

    no_alternative = json.loads(json.dumps(audit))
    no_alternative["bolgeler"]["uzunkuyu"]["saha_benzeri_ornekler"] = [
        candidate("Uzunkuyu", 38.28, 26.55, 0.42)
    ]
    preserved = select_final_diverse_calibration(no_alternative, report)
    assert [item["mahalle"] for item in preserved] == ["Reisdere", "Uzunkuyu"], preserved


if __name__ == "__main__":
    _self_check()
    chosen = update_final_diversity()
    print(
        "Nihai alarm-dışı saha çeşitliliği güncellendi: "
        + (", ".join(str(item.get("mahalle") or "?") for item in chosen) or "kalibrasyon yok")
    )
