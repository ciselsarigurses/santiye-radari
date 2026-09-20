"""13 Eylül ve 15 Eylül baseline'larını aynı post-onset S2 sahnesine karşılaştırır.

Amaç, saha-doğrulanmış kazı imzasına benzeyen ve tarla/bitki negatif bağlamından geçen
adaylarda müdahalenin 15 Eylül sonrasında da devam edip etmediğini exact koordinatta
ölçmektir. Bu, iki bağımsız sensör kanıtı değildir: iki S2 karşılaştırması aynı yeni
sahneyi paylaşır. Bu yüzden alarm, saha görevi veya rota üretmez; yalnız pozitif temporal
devamlılık diagnostiğidir. 250 m² ana eşik ile 150–249 m² MİKRO politikası değişmez.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import excavation_reference_signature_audit as signature
import localized_excavation_seed_audit as seed_audit
import post_onset_baseline_excavation_audit as long_base
import satellite
import seed_centered_excavation_morphology_audit as morphology


OUTPUT = Path(__file__).with_name("post_onset_dual_baseline_persistence_review.json")
VEGETATION_REVIEW = Path(__file__).with_name("post_onset_baseline_vegetation_guard_review.json")
SHORT_BASELINE_DATE = "2026-09-15"
POST_ONSET_AFTER = "2026-09-17"
REGION_KEYS = ("cesme", "uzunkuyu")
MAIN_THRESHOLD_M2 = 250
MICRO_MIN_M2 = 150
MICRO_MAX_M2 = 249
POSITIVE_LEVELS = {"ORTA", "YUKSEK"}


def _date(item):
    raw = str((item.get("properties") or {}).get("datetime") or "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _cloud(item):
    try:
        return float((item.get("properties") or {}).get("eo:cloud_cover"))
    except (TypeError, ValueError):
        return 999.0


def _full(item, bbox):
    assets = item.get("assets") or {}
    return satellite._item_covers_bbox(item, bbox) and all(
        key in assets for key in ("visual", "red", "nir", "scl")
    )


def _choose_short_pair(region_key):
    """15 Eylül exact baseline + en güncel 17 Eylül sonrası aynı-yörünge çifti."""
    bbox = satellite.REGIONS[region_key]["bbox"]
    items = satellite._search_items(bbox, days=30, max_cloud=100)
    baseline_day = datetime.fromisoformat(SHORT_BASELINE_DATE).date()
    post_day = datetime.fromisoformat(POST_ONSET_AFTER).date()

    baseline = [item for item in items if _full(item, bbox) and _date(item) == baseline_day]
    posts = [
        item for item in items
        if _full(item, bbox) and _date(item) is not None and _date(item) > post_day
    ]
    baseline.sort(key=_cloud)
    if not baseline:
        return None, "15_EYLUL_VERI_YOK"
    if not posts:
        return None, "POST_ONSET_VERI_YOK"

    for target_day in sorted({_date(item) for item in posts}, reverse=True):
        day_posts = sorted([item for item in posts if _date(item) == target_day], key=_cloud)
        for latest in day_posts:
            for older in baseline:
                if not satellite._same_mgrs_tile(older, latest):
                    continue
                old_orbit = satellite._relative_orbit(older)
                new_orbit = satellite._relative_orbit(latest)
                if old_orbit is None or new_orbit is None or old_orbit != new_orbit:
                    continue
                return (older, latest), "AYNI_MGRS_AYNI_YORUNGE"
    return None, "GEOMETRI_UYUMSUZ"


def _load_vegetation_review():
    try:
        payload = json.loads(VEGETATION_REVIEW.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _candidate_eval(candidate, arrays):
    latitude = float(candidate["enlem"])
    longitude = float(candidate["boylam"])
    row, col = signature._row_col(latitude, longitude, arrays["bbox"], arrays["shape"])
    metrics = seed_audit._window_metrics(arrays, row, col)
    seed_pass = seed_audit._passes(metrics)
    seed_class = seed_audit._pass_class(metrics)
    morph = morphology._score_at(arrays, latitude, longitude)
    level = str(morph.get("seed_merkezli_morfoloji_seviyesi") or "").upper()
    short_detected = bool(seed_pass and level in POSITIVE_LEVELS)
    area = int(candidate.get("spektral_etki_alani_m2") or 0)
    return {
        **candidate,
        "kisa_baseline_lokal_seed_uyumu": seed_pass,
        "kisa_baseline_seed_sinifi": seed_class,
        "kisa_baseline_morfoloji_puani": morph.get("seed_merkezli_morfoloji_puani"),
        "kisa_baseline_morfoloji_seviyesi": morph.get("seed_merkezli_morfoloji_seviyesi"),
        "15_post_s2_devamlilik": short_detected,
        "iki_s2_baseline_destegi": short_detected,
        "iki_s2_baseline_bagimsiz_sensor_kaniti": False,
        "ana_esik": area >= MAIN_THRESHOLD_M2,
        "mikro": MICRO_MIN_M2 <= area <= MICRO_MAX_M2,
        "alarm": False,
        "saha_gorevi": False,
        "kisa_baseline_metrikleri": metrics,
    }


def _critical_short_regression(arrays):
    rows = []
    for point in long_base.CRITICAL_POINTS:
        if not long_base._in_bbox(point, arrays["bbox"]):
            continue
        row, col = signature._row_col(
            point["enlem"], point["boylam"], arrays["bbox"], arrays["shape"]
        )
        metrics = seed_audit._window_metrics(arrays, row, col)
        seed_pass = seed_audit._passes(metrics)
        morph = morphology._score_at(arrays, point["enlem"], point["boylam"])
        detected = bool(
            seed_pass
            and str(morph.get("seed_merkezli_morfoloji_seviyesi") or "").upper()
            in POSITIVE_LEVELS
        )
        rows.append({
            "id": point["id"],
            "sonuc": point["sonuc"],
            "15_post_tespit": detected,
            "seed_sinifi": seed_audit._pass_class(metrics),
            "morfoloji_puani": morph.get("seed_merkezli_morfoloji_puani"),
            "morfoloji_seviyesi": morph.get("seed_merkezli_morfoloji_seviyesi"),
        })
    return rows


def _region(region_key, vegetation_payload):
    veg_region = ((vegetation_payload.get("bolgeler") or {}).get(region_key) or {})
    candidates = [
        item for item in (veg_region.get("korunan_adaylar") or [])
        if isinstance(item, dict) and "enlem" in item and "boylam" in item
    ]
    if not candidates:
        return {
            "durum": "ADAY_YOK",
            "bolge": satellite.REGIONS[region_key]["label"],
            "girdi_korunan_aday": 0,
            "alarm": False,
            "saha_gorevi": False,
        }

    pair, status = _choose_short_pair(region_key)
    if pair is None:
        return {
            "durum": status,
            "bolge": satellite.REGIONS[region_key]["label"],
            "girdi_korunan_aday": len(candidates),
            "alarm": False,
            "saha_gorevi": False,
        }

    arrays = long_base._arrays_for_pair(region_key, pair)
    expected_post = str(veg_region.get("post_onset_tarih") or "")
    scene_matches = not expected_post or expected_post == arrays["latest_date"]
    rows = [_candidate_eval(candidate, arrays) for candidate in candidates]
    if not scene_matches:
        for row in rows:
            row["iki_s2_baseline_destegi"] = False
            row["sahne_tarihi_eslesmiyor"] = True

    main_supported = [
        row for row in rows
        if row.get("iki_s2_baseline_destegi") and row.get("ana_esik")
    ]
    micro_supported = [
        row for row in rows
        if row.get("iki_s2_baseline_destegi") and row.get("mikro")
    ]
    return {
        "durum": "ok",
        "bolge": satellite.REGIONS[region_key]["label"],
        "uzun_baseline_tarih": veg_region.get("baseline_tarih"),
        "kisa_baseline_tarih": arrays["older_date"],
        "post_onset_tarih": arrays["latest_date"],
        "sahne_tarihi_eslesiyor": scene_matches,
        "girdi_korunan_aday": len(candidates),
        "iki_s2_baseline_destekli": sum(1 for row in rows if row.get("iki_s2_baseline_destegi")),
        "ana_esik_iki_s2_baseline_destekli": len(main_supported),
        "mikro_iki_s2_baseline_destekli": len(micro_supported),
        "kritik_15_post_regresyon": _critical_short_regression(arrays),
        "adaylar": rows,
        "alarm": False,
        "saha_gorevi": False,
    }


def _self_check():
    assert MAIN_THRESHOLD_M2 == 250
    assert (MICRO_MIN_M2, MICRO_MAX_M2) == (150, 249)
    assert len(long_base.CRITICAL_POINTS) == 5
    sample = {"spektral_etki_alani_m2": 250}
    assert int(sample["spektral_etki_alani_m2"]) >= MAIN_THRESHOLD_M2


def audit():
    _self_check()
    vegetation = _load_vegetation_review()
    regions = {}
    for key in REGION_KEYS:
        try:
            regions[key] = _region(key, vegetation)
        except Exception as exc:
            regions[key] = {
                "durum": "HATA",
                "bolge": satellite.REGIONS[key]["label"],
                "hata": f"{type(exc).__name__}: {exc}",
                "alarm": False,
                "saha_gorevi": False,
            }

    total_main = sum(
        int(region.get("ana_esik_iki_s2_baseline_destekli") or 0)
        for region in regions.values()
        if isinstance(region, dict)
    )
    total_micro = sum(
        int(region.get("mikro_iki_s2_baseline_destekli") or 0)
        for region in regions.values()
        if isinstance(region, dict)
    )
    return {
        "surum": 1,
        "amac": "13→post ve 15→aynı post S2 pencerelerinde gerçek-kazı benzeri müdahale devamlılığını exact koordinatta ölçmek",
        "gercek_derinlik_olcumu": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": [MICRO_MIN_M2, MICRO_MAX_M2],
        "iki_s2_baseline_bagimsiz_sensor_kaniti": False,
        "toplam_ana_esik_iki_s2_baseline_destekli": total_main,
        "toplam_mikro_iki_s2_baseline_destekli": total_micro,
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "bolgeler": regions,
        "not": (
            "Bu katman 13→post ve 15→aynı post Sentinel-2 karşılaştırmalarını çaprazlar. "
            "Aynı yeni sahneyi paylaştıkları için bağımsız sensör kanıtı sayılmaz ve tek başına "
            "KONTROLE_GIT üretmez. Tarla/bitki negatif bağlamı vegetation guard tarafından önceden "
            "bastırılmış aday havuzundan gelir."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("post onset dual baseline persistence self-check: ok")
        return 0
    payload = audit()
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
