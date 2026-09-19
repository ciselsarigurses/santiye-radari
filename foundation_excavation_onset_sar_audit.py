"""15-17 Eylül kazı başlangıç penceresi için Sentinel-1 RTC diagnostigi.

Bu katman yalnız diagnostiktir: alarm, saha görevi veya rota üretmez. Amaç mevcut
S2 lokal-seed/morfoloji havuzunu, mümkünse doğrudan 15-17 Eylül müdahale penceresini
örten aynı-geometrili Sentinel-1 RTC baseline→müdahale çiftiyle ayrıca ölçmektir.
Olmayan bir tarih veya çift uydurulmaz.

19 Eylül 2026 canlı veri gerçekliğinde kritik pencere denetimi, S2 için 16/17 Eylül
sahnelerinin bulunmadığını; S1 için ise 11→17 Eylül aynı-geometri baseline→müdahale
çiftinin mevcut olduğunu gösterdi. Bu dosya o S1 çiftini aday düzeyinde kullanır ve
1 doğrulanmış kazı + 4 çekirdek yanlış pozitifi aynı çiftte regresyon seti olarak
ölçer. Sonuçlar üretim eşiğini veya rota politikasını değiştirmez.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import math
from pathlib import Path

import foundation_excavation_sar_seed_audit as sar_seed
import sentinel1_rtc_change_diagnostic as rtc


SEED_REVIEW = Path(__file__).with_name("seed_centered_excavation_morphology_review.json")
FEEDBACK_JSON = Path(__file__).with_name("manual_field_feedback.json")
OUTPUT_JSON = Path(__file__).with_name("foundation_excavation_onset_sar_review.json")

ONSET_OLD = date(2026, 9, 11)
ONSET_NEW = date(2026, 9, 17)
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
REGION_KEYS = ("cesme", "uzunkuyu")
CORE_TRUE_ID = "FN-20260916-CESME-001"
CORE_FP_IDS = {
    "FP-20260916-REISDERE-001",
    "FP-20260917-UZUNKUYU-001",
    "FP-20260917-MUSALLA-001",
    "FP-20260917-MUSALLA-002",
}
CORE_IDS = {CORE_TRUE_ID, *CORE_FP_IDS}
BLOCK_RESULTS = {"YANLIS_POZITIF", "MEVCUT_MUSTERI"}
DEFAULT_MATCH_RADIUS_M = 30.0


def _load_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _feedback():
    payload = _load_json(FEEDBACK_JSON)
    return [item for item in payload.get("kayitlar", []) if isinstance(item, dict)]


def _item_date(item):
    dt = rtc.s1._iso_datetime(item)
    return dt.date() if dt is not None else None


def _onset_search(bbox, query_fn=rtc._query_rtc_items):
    """RTC sorgusundan yalnız 11 ve 17 Eylül sahnelerini geçir."""
    items = query_fn(bbox, days=30)
    return [item for item in items if _item_date(item) in {ONSET_OLD, ONSET_NEW}]


def _distance_m(a, b):
    lat1, lon1 = float(a[0]), float(a[1])
    lat2, lon2 = float(b[0]), float(b[1])
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _feedback_guard(row, feedback):
    try:
        point = (float(row["enlem"]), float(row["boylam"]))
    except (KeyError, TypeError, ValueError):
        return None
    matches = []
    for item in feedback:
        try:
            target = (float(item["enlem"]), float(item["boylam"]))
        except (KeyError, TypeError, ValueError):
            continue
        try:
            radius = float(item.get("eslesme_yaricapi_m") or DEFAULT_MATCH_RADIUS_M)
        except (TypeError, ValueError):
            radius = DEFAULT_MATCH_RADIUS_M
        distance = _distance_m(point, target)
        if distance > radius:
            continue
        matches.append(
            {
                "id": item.get("id"),
                "sonuc": str(item.get("sonuc") or "").upper(),
                "mesafe_m": round(distance, 1),
            }
        )
    if not matches:
        return None
    matches.sort(key=lambda item: item["mesafe_m"])
    return matches[0]


def _reference_targets(region_key, feedback):
    aoi = rtc.s1.AOIS[region_key]
    west, south, east, north = aoi["bbox"]
    targets = []
    for item in feedback:
        if item.get("id") not in CORE_IDS:
            continue
        try:
            lat = float(item["enlem"])
            lon = float(item["boylam"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (south <= lat <= north and west <= lon <= east):
            continue
        try:
            area = int(item.get("yaklasik_alan_m2") or MAIN_THRESHOLD_M2)
        except (TypeError, ValueError):
            area = MAIN_THRESHOLD_M2
        targets.append(
            {
                "enlem": lat,
                "boylam": lon,
                "alan_m2": max(MAIN_THRESHOLD_M2, area),
                "kaynak": "ONSET_SAR_REGRESYON",
                "regresyon_id": item.get("id"),
                "beklenen_sonuc": str(item.get("sonuc") or "").upper(),
                "s2_spektral_etki_alani_m2": item.get("yaklasik_alan_m2"),
            }
        )
    return targets


def _candidate_targets(region):
    candidates = sar_seed._select_candidates(region)
    return candidates, [sar_seed._target_from_candidate(item) for item in candidates]


def _exact_onset_pair(result):
    return bool(
        str(result.get("eski_tarih") or "") == ONSET_OLD.isoformat()
        and str(result.get("yeni_tarih") or "") == ONSET_NEW.isoformat()
    )


def _summarize_row(row, pair_ok, feedback):
    source = str(row.get("kaynak") or "")
    is_reference = source == "ONSET_SAR_REGRESYON"
    strong = bool(pair_ok and sar_seed._strong_local(row))
    guard = None if is_reference else _feedback_guard(row, feedback)
    blocked = bool(guard and guard.get("sonuc") in BLOCK_RESULTS)
    try:
        effect = int(row.get("s2_spektral_etki_alani_m2") or 0)
    except (TypeError, ValueError):
        effect = 0
    main = effect >= MAIN_THRESHOLD_M2
    micro = MICRO_RANGE_M2[0] <= effect <= MICRO_RANGE_M2[1]
    return {
        "enlem": row.get("enlem"),
        "boylam": row.get("boylam"),
        "kaynak": source or None,
        "regresyon_id": row.get("regresyon_id"),
        "beklenen_sonuc": row.get("beklenen_sonuc"),
        "spektral_etki_alani_m2": effect,
        "s2_seed_morfoloji_puani": row.get("s2_seed_morfoloji_puani"),
        "s2_seed_morfoloji_seviyesi": row.get("s2_seed_morfoloji_seviyesi"),
        "rtc_cifti_kapsiyor": row.get("rtc_cifti_kapsiyor"),
        "sar_metrik_var": bool(row.get("polarizasyon_metrikleri")),
        "sar_okuma_hatalari": row.get("okuma_hatalari") or None,
        "sar_degisim_durumu": row.get("sar_degisim_durumu"),
        "sar_lokal_degisim_skor_db": row.get("sar_lokal_degisim_skor_db"),
        "sar_polarizasyon_uyumu": row.get("sar_polarizasyon_uyumu"),
        "sar_mekansal_ayrim": row.get("sar_mekansal_ayrim"),
        "sar_cevre_degisim_tepe_db": row.get("sar_cevre_degisim_tepe_db"),
        "onset_sar_guclu_lokal_destek": strong,
        "saha_kalibrasyon_koruma": guard,
        "geri_bildirim_engeli": blocked,
        "ana_esik": main,
        "mikro_aralik": micro,
        "onset_ana_destekli_diagnostik": bool(strong and main and not blocked and not is_reference),
        "onset_mikro_destekli_diagnostik": bool(strong and micro and not blocked and not is_reference),
        "alarm": False,
        "saha_gorevi": False,
    }


def _regression_summary(rows):
    output = []
    by_id = {row.get("regresyon_id"): row for row in rows if row.get("regresyon_id")}
    for item_id in [CORE_TRUE_ID, *sorted(CORE_FP_IDS)]:
        row = by_id.get(item_id)
        expected_positive = item_id == CORE_TRUE_ID
        detected = bool(row and row.get("onset_sar_guclu_lokal_destek"))
        output.append(
            {
                "id": item_id,
                "beklenen": "DOGRULANMIS_KAZI" if expected_positive else "YANLIS_POZITIF",
                "olculdu": row is not None,
                "onset_sar_guclu_lokal_destek": detected if row is not None else None,
                "sar_lokal_degisim_skor_db": row.get("sar_lokal_degisim_skor_db") if row else None,
                "sar_mekansal_ayrim": row.get("sar_mekansal_ayrim") if row else None,
                "regresyon_uyumlu": bool(row is not None and detected == expected_positive),
            }
        )
    return output


def _analyze_region(region_key, region, feedback):
    candidates, candidate_targets = _candidate_targets(region)
    reference_targets = _reference_targets(region_key, feedback)
    targets = candidate_targets + reference_targets
    result = rtc.inspect_region(region_key, targets, search_fn=_onset_search)
    pair_ok = _exact_onset_pair(result)
    rows = [_summarize_row(row, pair_ok, feedback) for row in (result.get("hedefler") or [])]
    candidate_rows = [row for row in rows if row.get("kaynak") != "ONSET_SAR_REGRESYON"]
    reference_rows = [row for row in rows if row.get("kaynak") == "ONSET_SAR_REGRESYON"]
    main_supported = [row for row in candidate_rows if row.get("onset_ana_destekli_diagnostik")]
    micro_supported = [row for row in candidate_rows if row.get("onset_mikro_destekli_diagnostik")]
    main_supported.sort(key=lambda row: float(row.get("sar_lokal_degisim_skor_db") or -1), reverse=True)
    micro_supported.sort(key=lambda row: float(row.get("sar_lokal_degisim_skor_db") or -1), reverse=True)
    return {
        "bolge": region.get("bolge") or region_key,
        "durum": result.get("durum"),
        "istenen_sar_cifti": f"{ONSET_OLD.isoformat()}->{ONSET_NEW.isoformat()}",
        "sar_eski_tarih": result.get("eski_tarih"),
        "sar_yeni_tarih": result.get("yeni_tarih"),
        "tam_onset_cifti": pair_ok,
        "s2_seed_havuz_sayisi": len(region.get("adaylar") or []),
        "s2_seed_hedef_sayisi": len(candidate_targets),
        "regresyon_hedef_sayisi": len(reference_targets),
        "hedef_sayisi": len(rows),
        "sar_metrik_uretilen_hedef": sum(1 for row in rows if row.get("sar_metrik_var")),
        "sar_okuma_hatasi_olan_hedef": sum(1 for row in rows if row.get("sar_okuma_hatalari")),
        "onset_ana_destekli_sayi": len(main_supported),
        "onset_mikro_destekli_sayi": len(micro_supported),
        "onset_ana_destekli_adaylar": main_supported[:20],
        "onset_mikro_destekli_adaylar": micro_supported[:20],
        "regresyon_sonuclari": _regression_summary(reference_rows),
        "alarm": False,
        "saha_gorevi": False,
    }


def _self_check():
    fake = lambda day: {
        "id": f"S1_{day}",
        "properties": {"datetime": f"{day}T16:00:00Z"},
    }
    filtered = _onset_search(
        [26.2, 38.1, 26.6, 38.5],
        query_fn=lambda bbox, days=30: [
            fake("2026-09-11"),
            fake("2026-09-12"),
            fake("2026-09-17"),
            fake("2026-09-18"),
        ],
    )
    assert [_item_date(item) for item in filtered] == [ONSET_OLD, ONSET_NEW]
    feedback = [
        {"id": "FP", "sonuc": "YANLIS_POZITIF", "enlem": 38.3, "boylam": 26.3, "eslesme_yaricapi_m": 30}
    ]
    guard = _feedback_guard({"enlem": 38.30001, "boylam": 26.30001}, feedback)
    assert guard and guard["id"] == "FP"
    assert _feedback_guard({"enlem": 38.31, "boylam": 26.31}, feedback) is None


def audit():
    _self_check()
    review = _load_json(SEED_REVIEW)
    feedback = _feedback()
    regions = {}
    regression = []
    total_main = 0
    total_micro = 0
    for region_key in REGION_KEYS:
        region = (review.get("bolgeler") or {}).get(region_key) or {}
        try:
            regions[region_key] = _analyze_region(region_key, region, feedback)
            total_main += int(regions[region_key].get("onset_ana_destekli_sayi") or 0)
            total_micro += int(regions[region_key].get("onset_mikro_destekli_sayi") or 0)
            regression.extend(regions[region_key].get("regresyon_sonuclari") or [])
        except Exception as exc:
            regions[region_key] = {
                "bolge": region.get("bolge") or region_key,
                "durum": "HATA",
                "hata": f"{type(exc).__name__}: {exc}",
                "alarm": False,
                "saha_gorevi": False,
            }

    measured = [row for row in regression if row.get("olculdu")]
    unique = {row["id"]: row for row in measured}
    core_complete = CORE_IDS.issubset(unique)
    core_pass = bool(core_complete and all(unique[item_id]["regresyon_uyumlu"] for item_id in CORE_IDS))
    return {
        "surum": 1,
        "amac": "S2 lokal-seed/morfoloji havuzunu 11→17 Eylül Sentinel-1 RTC başlangıç-penceresi imzasıyla diagnostik olarak çaprazlamak",
        "gercek_derinlik_olcumu": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "istenen_onset_sar_cifti": f"{ONSET_OLD.isoformat()}->{ONSET_NEW.isoformat()}",
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "toplam_onset_ana_destekli": total_main,
        "toplam_onset_mikro_destekli": total_micro,
        "kritik_regresyon_tamam": core_complete,
        "kritik_regresyon_geciyor": core_pass,
        "kritik_regresyon": [unique[item_id] for item_id in [CORE_TRUE_ID, *sorted(CORE_FP_IDS)] if item_id in unique],
        "bolgeler": regions,
        "not": (
            "Bu çıktı yalnız diagnostiktir. 11→17 aynı-geometri RTC çifti gerçekten bulunmazsa tam_onset_cifti=false olur ve hiçbir aday onset desteği sayılmaz. "
            "Tek başına BSI/toprak değişimi veya tek SAR sıçraması KONTROLE_GIT üretmez. 250 m² ana eşik ile 150–249 m² MİKRO politikası değişmez."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("foundation excavation onset SAR self-check: ok")
        return 0
    payload = audit()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
