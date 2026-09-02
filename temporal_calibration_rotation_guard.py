"""Temporal kuru-zemin kalibrasyonunun aynı güçlü noktaya kilitlenmesini önler.

`calibration_rotation_guard.py` normal kuru-zemin adaylarını aynı Sentinel sahnesi
kaldıkça döndürür; ancak zaman-serisinde ani başlangıç desteği alan adaylar her gün
normal rotasyonun önüne sabitlenebilir. Bu, saha etiketi gelmediğinde aynı güçlü
alarm-dışı noktayı tekrar tekrar gösterip diğer mahallelerden kalibrasyon verisi
toplamayı geciktirir.

Bu koruma yalnız mevcut ALARM_DEGIL kalibrasyon slotlarını yeniden sıralar. Üretim
alarmı, görev, 250 m² eşiği veya Sentinel ana filtresi değişmez. Aynı rapor günü ve
aynı Sentinel çifti için temporal kanıt varsa güçlü ani-başlangıç adayları da en fazla
dört kişilik mahalle-çeşitli havuzda gün gün döner. 250-900 m² adayda güncel yerellik
denetimi çevre değişiminin yaygın olduğunu güvenilir biçimde gösteriyorsa aday temporal
"acil" havuzundan düşürülür; normal güvenli rotasyonda kalmaya devam eder. Aynı sınıfta
3x3 merkez değişimi 5x5 çevre halkasına göre belirgin biçimde lokal kalan adaylar,
saha kalibrasyonunda lokal kanıtı olmayan ani-başlangıç adaylarının önüne alınır.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import calibration_rotation_guard as calibration


LOCALITY_AUDIT = Path(__file__).with_name("temporal_locality_audit.json")
MAX_LOCALITY_AREA_M2 = 900


def _load_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _locality_region_map(locality_payload, region_key, region_data, report_date):
    """Yalnız aynı gün ve birebir aynı Sentinel çiftindeki yerellik kanıtını kabul et."""
    if not isinstance(locality_payload, dict) or not isinstance(region_data, dict):
        return {}
    if str(locality_payload.get("rapor_tarihi") or "") != str(report_date or ""):
        return {}

    regions = locality_payload.get("bolgeler") or {}
    locality_region = regions.get(region_key) if isinstance(regions, dict) else None
    if not isinstance(locality_region, dict) or locality_region.get("durum") != "ok":
        return {}

    if str(locality_region.get("onceki_item") or "") != str(region_data.get("onceki_item") or ""):
        return {}
    if str(locality_region.get("son_item") or "") != str(region_data.get("son_item") or ""):
        return {}

    mapped = {}
    for raw in locality_region.get("adaylar") or []:
        if not isinstance(raw, dict):
            continue
        key = calibration._point_key(raw)
        if key is not None:
            mapped[key] = dict(raw)
    return mapped


def _attach_locality_evidence(items, locality_map):
    decorated = []
    for item in items:
        updated = dict(item)
        raw = locality_map.get(calibration._point_key(item)) if locality_map else None
        if isinstance(raw, dict):
            updated["yerellik_lokal_ani_baslangic_destegi"] = bool(
                raw.get("lokal_ani_baslangic_destegi")
            )
            updated["yerellik_yaygin_cevre_degisim_riski"] = bool(
                raw.get("yaygin_cevre_degisim_riski")
            )
            updated["yerellik_orani"] = calibration.route._number(
                raw.get("yerellik_orani"), 0
            )
            updated["yerellik_cevre_halka_son_bsi_degisim"] = raw.get(
                "cevre_halka_son_bsi_degisim"
            )
            updated["yerellik_cevre_halka_gecerli_oran"] = raw.get(
                "cevre_halka_gecerli_oran"
            )
        decorated.append(updated)
    return decorated


def _small_widespread_risk(item):
    area = calibration.route._number(item.get("alan_m2"), 0)
    return bool(
        250 <= area <= MAX_LOCALITY_AREA_M2
        and item.get("yerellik_yaygin_cevre_degisim_riski")
    )


def _temporal_urgent(item):
    return bool(
        item.get("zaman_serisi_ani_baslangic_destegi")
        and not item.get("zaman_serisi_istikrarsiz_zemin_riski")
        and not _small_widespread_risk(item)
    )


def _ordered_candidates(ranked, age_days):
    """Lokal ani başlangıcı öne alıp güçlü havuzu günlük döndür; sonra normale dön."""
    urgent = [item for item in ranked if _temporal_urgent(item)]
    urgent.sort(
        key=lambda item: (
            not bool(item.get("yerellik_lokal_ani_baslangic_destegi")),
            -calibration.route._number(item.get("zaman_serisi_ani_baslangic_orani"), 0),
            -calibration.route._number(item.get("yerellik_orani"), 0),
            -abs(calibration.route._number(item.get("ortalama_bsi_degisim"), 0)),
            calibration.route._number(item.get("alan_m2"), 0),
        )
    )
    urgent_rotated = calibration._rotated_order(
        urgent,
        age_days,
        pool_size=calibration.ROTATION_POOL_PER_REGION,
    )

    normal_rotated = calibration._rotated_order(
        ranked,
        age_days,
        pool_size=calibration.ROTATION_POOL_PER_REGION,
    )
    urgent_keys = {
        calibration._point_key(item)
        for item in urgent_rotated
        if calibration._point_key(item) is not None
    }
    return urgent_rotated + [
        item for item in normal_rotated
        if calibration._point_key(item) not in urgent_keys
    ]


def _existing_by_region(report_payload):
    values = []
    seen = set()
    for item in (report_payload or {}).get("kuru_zemin_kalibrasyon_kontrolu") or []:
        if not isinstance(item, dict):
            continue
        region_key = str(item.get("bolge_anahtari") or "")
        if not region_key or region_key in seen:
            continue
        seen.add(region_key)
        values.append((region_key, dict(item)))
    return values


def select_temporal_rotation(audit_payload, report_payload, temporal_payload, locality_payload):
    """Mevcut kalibrasyon slotlarını koruyarak temporal önceliği günlük döndür."""
    if not isinstance(audit_payload, dict) or not isinstance(report_payload, dict):
        return []

    current = [
        dict(item)
        for item in report_payload.get("kuru_zemin_kalibrasyon_kontrolu") or []
        if isinstance(item, dict)
    ]
    if not current:
        return []

    regions = audit_payload.get("bolgeler") or {}
    if not isinstance(regions, dict):
        return current

    active = calibration.route._actionable_candidates(
        report_payload.get("saha_adaylari") or []
    )
    report_date = report_payload.get("rapor_tarihi") or audit_payload.get("rapor_tarihi")
    selected = []
    processed = set()

    for region_key, original in _existing_by_region(report_payload):
        region_data = regions.get(region_key)
        if not isinstance(region_data, dict):
            selected.append(dict(original))
            processed.add(region_key)
            continue

        ranked = calibration._eligible_region_items(region_key, region_data, active)
        temporal_map = calibration._temporal_region_map(
            temporal_payload,
            region_key,
            region_data,
            report_date,
        )
        if not ranked or not temporal_map:
            selected.append(dict(original))
            processed.add(region_key)
            continue

        ranked = calibration._attach_temporal_evidence(ranked, temporal_map)
        locality_map = _locality_region_map(
            locality_payload,
            region_key,
            region_data,
            report_date,
        )
        ranked = _attach_locality_evidence(ranked, locality_map)
        age_days = calibration._days_since_scene(report_date, region_data.get("son_tarih"))
        ordered = _ordered_candidates(ranked, age_days)
        picked = next(
            (
                item for item in ordered
                if calibration.route._far_from_selected(item, selected)
            ),
            None,
        )
        if picked is None:
            selected.append(dict(original))
            processed.add(region_key)
            continue

        chosen = dict(picked)
        chosen["kalibrasyon_rotasyon_gun"] = int(age_days)
        chosen["kalibrasyon_rotasyon_havuzu"] = min(
            len(ranked), calibration.ROTATION_POOL_PER_REGION
        )
        if bool(chosen.get("zaman_serisi_ani_baslangic_destegi")):
            if bool(chosen.get("yerellik_lokal_ani_baslangic_destegi")):
                chosen["kalibrasyon_nedeni"] = "ZAMAN_SERISI_LOKAL_ANI_BASLANGIC_ROTASYON"
            else:
                chosen["kalibrasyon_nedeni"] = "ZAMAN_SERISI_ANI_BASLANGIC_ROTASYON"
        selected.append(chosen)
        processed.add(region_key)

    # Bilinmeyen/eski kaydı silme; bu katman slot açmaz veya kapatmaz.
    for item in current:
        region_key = str(item.get("bolge_anahtari") or "")
        if region_key and region_key in processed:
            continue
        selected.append(dict(item))

    assert len(selected) == len(current), (
        "Temporal kalibrasyon rotasyonu mevcut kalibrasyon slot sayısını değiştirdi."
    )
    assert all(item.get("kalibrasyon_durumu") == "ALARM_DEGIL" for item in selected), (
        "Temporal kalibrasyon rotasyonu alarm-dışı statüyü bozdu."
    )
    return selected


def update_temporal_rotation():
    audit = _load_json(calibration.DRY_GROUND_AUDIT)
    report = _load_json(calibration.REPORT_JSON)
    temporal_payload = _load_json(calibration.DRY_GROUND_TEMPORAL_AUDIT)
    locality_payload = _load_json(LOCALITY_AUDIT)
    if audit is None or report is None or temporal_payload is None:
        return []

    selected = select_temporal_rotation(
        audit,
        report,
        temporal_payload,
        locality_payload,
    )
    report["kuru_zemin_kalibrasyon_kontrolu"] = selected
    report["temporal_kalibrasyon_rotasyonu"] = {
        "durum": "uygulandi",
        "slot_sayisi": len(selected),
        "kural": (
            "Ani-baslangic destekli alarm-disi kalibrasyonlar ayni Sentinel sahnesinde "
            "en guclu dort mahalle-cesitli aday arasinda gunluk doner; 250-900 m2 "
            "adayda guvenilir yaygin cevre degisim riski temporal acil havuzundan "
            "dusurulur. Ayni boyut sinifinda merkez degisimi cevre halkasina gore "
            "belirgin lokal kalan adaylar saha kalibrasyonunda once gelir; normal "
            "guvenli rotasyon korunur."
        ),
    }
    calibration.REPORT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
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

    rows = [
        candidate("W1", 38.20, 26.30, 0.40),
        candidate("W2", 38.22, 26.32, 0.35),
        candidate("W3", 38.24, 26.34, 0.30),
        candidate("W4", 38.26, 26.36, 0.25),
        candidate("W5", 38.28, 26.38, 0.20),
    ]
    audit = {
        "rapor_tarihi": "2026-09-01",
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "bolge": "Batı",
                "onceki_item": "OLD",
                "son_item": "NEW",
                "onceki_tarih": "29.08.2026",
                "son_tarih": "29.08.2026",
                "saha_benzeri_ornekler": rows,
            }
        },
    }
    report = {
        "rapor_tarihi": "2026-09-01",
        "saha_adaylari": [],
        "kuru_zemin_kalibrasyon_kontrolu": [
            {
                **rows[0],
                "bolge_anahtari": "cesme",
                "kalibrasyon_durumu": "ALARM_DEGIL",
            }
        ],
    }
    temporal_rows = []
    for index, row in enumerate(rows):
        temporal_rows.append(
            {
                **row,
                "ani_baslangic_destegi": True,
                "ani_baslangic_orani": 10 - index,
                "onceki_donem_bsi_degisim": 0.02,
                "onceki_donem_gecerli_oran": 1.0,
                "istikrarsiz_zemin_riski": False,
            }
        )
    temporal_payload = {
        "rapor_tarihi": "2026-09-01",
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "onceki_tarih": "29.08.2026",
                "son_tarih": "29.08.2026",
                "adaylar": temporal_rows,
            }
        },
    }
    locality_payload = {
        "rapor_tarihi": "2026-09-01",
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "onceki_item": "OLD",
                "son_item": "NEW",
                "adaylar": [
                    {
                        **rows[3],
                        "lokal_ani_baslangic_destegi": False,
                        "yaygin_cevre_degisim_riski": True,
                        "yerellik_orani": 1.0,
                    }
                ],
            }
        },
    }

    # Sahne yaşı 3 gün: W4 normalde temporal ilk dörtlünün 4. adayı olurdu; fakat
    # güvenilir yaygın çevre değişimi riski nedeniyle acil havuzdan çıkar. Kalan
    # W1/W2/W3/W5 dört havuzunda 3 günlük rotasyon W5'i seçmelidir.
    rotated = select_temporal_rotation(
        audit,
        report,
        temporal_payload,
        locality_payload,
    )
    assert rotated and rotated[0]["mahalle"] == "W5", rotated
    assert rotated[0]["kalibrasyon_durumu"] == "ALARM_DEGIL"
    assert rotated[0]["kalibrasyon_nedeni"] == "ZAMAN_SERISI_ANI_BASLANGIC_ROTASYON"

    # Yeni sahnenin ilk gününde en güçlü temporal aday yine önce gelmeli.
    fresh_report = json.loads(json.dumps(report))
    fresh_report["rapor_tarihi"] = "2026-08-29"
    fresh_temporal = json.loads(json.dumps(temporal_payload))
    fresh_temporal["rapor_tarihi"] = "2026-08-29"
    fresh_locality = json.loads(json.dumps(locality_payload))
    fresh_locality["rapor_tarihi"] = "2026-08-29"
    fresh = select_temporal_rotation(
        audit,
        fresh_report,
        fresh_temporal,
        fresh_locality,
    )
    assert fresh and fresh[0]["mahalle"] == "W1", fresh

    # Aynı temporal sınıfta lokal kanıt, daha yüksek oranlı fakat yerellik desteği
    # olmayan adayı saha kalibrasyonunda geçmelidir. Bu yalnız ALARM_DEGIL sırasıdır.
    local_first = _ordered_candidates(
        [
            {
                **rows[0],
                "zaman_serisi_ani_baslangic_destegi": True,
                "zaman_serisi_istikrarsiz_zemin_riski": False,
                "zaman_serisi_ani_baslangic_orani": 20.0,
                "yerellik_lokal_ani_baslangic_destegi": False,
                "yerellik_orani": 1.2,
            },
            {
                **rows[1],
                "zaman_serisi_ani_baslangic_destegi": True,
                "zaman_serisi_istikrarsiz_zemin_riski": False,
                "zaman_serisi_ani_baslangic_orani": 5.0,
                "yerellik_lokal_ani_baslangic_destegi": True,
                "yerellik_orani": 1.8,
            },
        ],
        0,
    )
    assert local_first and local_first[0]["mahalle"] == "W2", local_first

    # Eski temporal çıktı varsa mevcut güvenli seçim korunmalı.
    stale_temporal = json.loads(json.dumps(temporal_payload))
    stale_temporal["rapor_tarihi"] = "2026-08-31"
    stale = select_temporal_rotation(
        audit,
        report,
        stale_temporal,
        locality_payload,
    )
    assert stale and stale[0]["mahalle"] == "W1", stale
    assert len(stale) == len(report["kuru_zemin_kalibrasyon_kontrolu"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Temporal kalibrasyon rotasyonu öz testi başarılı.")
        return 0
    selected = update_temporal_rotation()
    print(
        "Temporal kalibrasyon rotasyonu tamamlandı: "
        + (", ".join(str(item.get("mahalle") or "?") for item in selected) or "değişiklik yok")
        + ". Alarm/görev üretilmedi."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())