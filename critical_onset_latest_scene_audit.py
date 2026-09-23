"""15-17 Eylül başlangıç penceresini en güncel Sentinel sahnesiyle tamamlar.

Bu katman yalnız metadata/karşılaştırılabilirlik diagnostiğidir. Alarm veya saha görevi
üretmez; 250 m² ana eşiği ve 150-249 m² MİKRO politikasını değiştirmez. Exact
13→15, 15→16, 15→17 ve 16→17 farkları `critical_onset_window_audit.py` içinde
aynen korunur. Buradaki amaç, exact tarihler eksik olsa bile 15 Eylül öncesi
baseline ile gerçekten mevcut EN GÜNCEL post-window Sentinel-2/Sentinel-1 sahnesini
ayrı olarak kaydetmektir. Sentinel-2'de katalogdaki en yeni sahne üretim bulut
eşiğine takılıyorsa bu sahne yine kaydedilir, fakat doğrudan hesaplanabilir sayılmaz;
üretim filtresini geçen en güncel sahne ayrıca belirtilir. Olmayan tarih varmış gibi
davranılmaz.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path

import requests

import satellite
import sentinel1_scene_probe as s1


START = "2026-09-12T00:00:00Z"
BASELINE_DAY = date(2026, 9, 13)
INTERVENTION_START = date(2026, 9, 15)
REGION_KEYS = ("cesme", "uzunkuyu")
OUTPUT = Path(__file__).with_name("critical_onset_latest_scene.json")
# satellite._search_items üretimde ``eo:cloud_cover < 25`` kullanır. Bu metadata
# diagnostiği katalogdaki daha yeni yüksek-bulut sahneyi saklamaya devam eder,
# ancak onu doğrudan analiz/temporal fark için kullanılabilir ilan etmez.
S2_PRODUCTION_MAX_CLOUD = 25.0


def _iso_date(item):
    value = str((item.get("properties") or {}).get("datetime") or "")
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _cloud(item):
    value = (item.get("properties") or {}).get("eo:cloud_cover")
    try:
        return float(value) if value is not None else 999.0
    except (TypeError, ValueError):
        return 999.0


def _query_s2_all(bbox, request_post=requests.post):
    end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "collections": ["sentinel-2-c1-l2a"],
        "bbox": list(map(float, bbox)),
        "datetime": f"{START}/{end}",
        "limit": 100,
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    response = request_post(satellite.EARTH_SEARCH_URL, json=payload, timeout=45)
    response.raise_for_status()
    usable = []
    for item in response.json().get("features", []):
        assets = item.get("assets") or {}
        if not all(key in assets for key in ("visual", "red", "nir", "scl")):
            continue
        if not satellite._item_covers_bbox(item, bbox):
            continue
        if _iso_date(item) is None:
            continue
        usable.append(item)
    return usable


def _best_by_day(items):
    grouped = {}
    for item in items:
        day = _iso_date(item)
        if day is None:
            continue
        current = grouped.get(day)
        if current is None or _cloud(item) < _cloud(current):
            grouped[day] = item
    return grouped


def _s2_pair(baseline, newer, newer_day, require_production_cloud=False):
    if baseline is None or newer is None or newer_day is None:
        return None
    same_tile = satellite._same_mgrs_tile(baseline, newer)
    old_orbit = satellite._relative_orbit(baseline)
    new_orbit = satellite._relative_orbit(newer)
    newer_cloud = _cloud(newer)
    high_cloud = newer_cloud >= S2_PRODUCTION_MAX_CLOUD
    calculable = bool(same_tile and (not require_production_cloud or not high_cloud))
    if not same_tile:
        reason = "FARKLI_MGRS_KARO"
    elif require_production_cloud and high_cloud:
        reason = "YUKSEK_GLOBAL_BULUT_YEREL_SCL_DOGRULAMASI_GEREKLI"
    else:
        reason = "OK"
    return {
        "hesaplanabilir": calculable,
        "neden": reason,
        "baseline_tarihi": BASELINE_DAY.isoformat(),
        "son_tarih": newer_day.isoformat(),
        "eski_item": baseline.get("id"),
        "yeni_item": newer.get("id"),
        "eski_bulut_yuzde": round(_cloud(baseline), 2),
        "yeni_bulut_yuzde": round(newer_cloud, 2),
        "ayni_mgrs_karo": bool(same_tile),
        "ayni_goreli_yorunge": (
            old_orbit == new_orbit
            if old_orbit is not None and new_orbit is not None
            else None
        ),
        "yerel_scl_dogrulamasi_gerekli": bool(require_production_cloud and high_cloud),
    }


def _s2_summary(region_key, query_fn=_query_s2_all):
    bbox = satellite.REGIONS[region_key]["bbox"]
    try:
        grouped = _best_by_day(query_fn(bbox))
    except Exception as exc:
        return {"durum": "DIS_KAYNAK_HATASI", "hata": type(exc).__name__}

    post_days = sorted(day for day in grouped if day >= INTERVENTION_START)
    latest_day = post_days[-1] if post_days else None
    production_days = [
        day for day in post_days if _cloud(grouped[day]) < S2_PRODUCTION_MAX_CLOUD
    ]
    latest_production_day = production_days[-1] if production_days else None
    baseline = grouped.get(BASELINE_DAY)
    latest = grouped.get(latest_day) if latest_day else None
    latest_production = grouped.get(latest_production_day) if latest_production_day else None

    return {
        "durum": "ok",
        # Katalog gerçekliği: yüksek bulutlu olsa da gerçekten mevcut en yeni tarih.
        "en_guncel_postwindow_tarih": latest_day.isoformat() if latest_day else None,
        "en_guncel_postwindow_global_bulut_yuzde": (
            round(_cloud(latest), 2) if latest is not None else None
        ),
        # Üretim gerçekliği: satellite._search_items ile aynı strict <25 politikası.
        "en_guncel_uretim_filtresi_uygun_tarih": (
            latest_production_day.isoformat() if latest_production_day else None
        ),
        "uretim_global_bulut_esigi_yuzde": S2_PRODUCTION_MAX_CLOUD,
        "baseline_13eylul_mevcut": baseline is not None,
        "baseline_vs_en_guncel": _s2_pair(
            baseline,
            latest,
            latest_day,
            require_production_cloud=True,
        ),
        "baseline_vs_en_guncel_uretim_filtresi_uygun": _s2_pair(
            baseline,
            latest_production,
            latest_production_day,
            require_production_cloud=True,
        ),
    }


def _s1_items(region_key, search_fn=s1._search_items):
    result = search_fn(s1.AOIS[region_key]["bbox"], days=30)
    if isinstance(result, dict):
        return result.get("items") or [], result.get("kaynak"), result.get("kaynak_hatalari") or []
    return result or [], "test", []


def _s1_latest_pair(items, region_key):
    bbox = s1.AOIS[region_key]["bbox"]
    usable = [item for item in items if _iso_date(item) is not None and s1._usable_item(item, bbox)]
    post_days = sorted({_iso_date(item) for item in usable if _iso_date(item) >= INTERVENTION_START}, reverse=True)
    baseline_days = sorted({_iso_date(item) for item in usable if _iso_date(item) < INTERVENTION_START}, reverse=True)
    latest_day = post_days[0] if post_days else None

    for new_day in post_days:
        new_items = [item for item in usable if _iso_date(item) == new_day]
        for newer in new_items:
            signature = s1._compatible_signature(newer)
            new_points = set(s1._covered_points(newer, region_key))
            if signature is None or not new_points:
                continue
            for old_day in baseline_days:
                for older in [item for item in usable if _iso_date(item) == old_day]:
                    if s1._compatible_signature(older) != signature:
                        continue
                    common = sorted(new_points.intersection(s1._covered_points(older, region_key)))
                    if not common:
                        continue
                    return {
                        "en_guncel_postwindow_tarih": latest_day.isoformat() if latest_day else None,
                        "karsilastirilan_postwindow_tarih": new_day.isoformat(),
                        "hesaplanabilir": True,
                        "neden": "OK_AYNI_GEOMETRI",
                        "baseline_tarihi": old_day.isoformat(),
                        "eski_item": older.get("id"),
                        "yeni_item": newer.get("id"),
                        "ortak_kritik_noktalar": common,
                        "goreli_yorunge": s1._relative_orbit(newer),
                        "orbit_yonu": s1._orbit_state(newer),
                        "polarizasyon": list(s1._polarizations(newer)),
                    }
    return {
        "en_guncel_postwindow_tarih": latest_day.isoformat() if latest_day else None,
        "hesaplanabilir": False,
        "neden": "KARSILASTIRILABILIR_BASELINE_EN_GUNCEL_S1_CIFTI_YOK",
    }


def _s1_summary(region_key, search_fn=s1._search_items):
    try:
        items, source, errors = _s1_items(region_key, search_fn)
    except Exception as exc:
        return {"durum": "DIS_KAYNAK_HATASI", "hata": type(exc).__name__}
    return {
        "durum": "ok",
        "metadata_kaynagi": source,
        "kaynak_hatalari": errors,
        "baseline_vs_en_guncel": _s1_latest_pair(items, region_key),
    }


def build_report(s2_query=_query_s2_all, s1_search=s1._search_items):
    return {
        "surum": 1,
        "olusturma_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "amac": "15 Eylül öncesi baseline ile gerçekten mevcut en güncel post-window Sentinel sahnesini kaydetmek",
        "alarm": False,
        "saha_gorevi": False,
        "gercek_derinlik_olcumu": False,
        "ana_sentinel_esigi_m2": satellite.MIN_HOTSPOT_AREA_M2,
        "mikro_aralik_m2": [150, 249],
        "bolgeler": {
            key: {
                "etiket": satellite.REGIONS[key]["label"],
                "sentinel2": _s2_summary(key, s2_query),
                "sentinel1": _s1_summary(key, s1_search),
            }
            for key in REGION_KEYS
        },
        "not": "Exact 13→15, 15→16, 15→17 ve 16→17 uygunluğu ayrı kritik-pencere denetiminde kalır. Bu dosya katalogdaki en güncel gerçek sahneyi kaydeder; S2 global bulut üretim eşiğini aşarsa doğrudan hesaplanabilir saymaz ve üretim filtresini geçen en güncel tarihi ayrıca gösterir. Olmayan tarih üretilmez.",
    }


def _fake_s2(day, item_id, cloud=5.0, orbit=8):
    bbox = satellite.REGIONS["cesme"]["bbox"]
    return {
        "id": item_id,
        "bbox": list(bbox),
        "properties": {
            "datetime": f"{day}T09:00:00Z",
            "eo:cloud_cover": cloud,
            "s2:mgrs_tile": "35SMC",
            "sat:relative_orbit": orbit,
        },
        "assets": {key: {} for key in ("visual", "red", "nir", "scl")},
    }


def _fake_s1(day, orbit=29):
    bbox = s1.AOIS["cesme"]["bbox"]
    return {
        "id": f"S1_{day}",
        "bbox": list(bbox),
        "properties": {
            "datetime": f"{day}T16:00:00Z",
            "sat:relative_orbit": orbit,
            "sat:orbit_state": "ascending",
            "sar:polarizations": ["VV", "VH"],
            "sar:instrument_mode": "IW",
        },
    }


def _self_check():
    def s2_query(_bbox):
        return [
            _fake_s2("2026-09-13", "S2_13", cloud=0.5),
            _fake_s2("2026-09-15", "S2_15", cloud=11.0),
            _fake_s2("2026-09-18", "S2_18", cloud=0.3),
            _fake_s2("2026-09-23", "S2_23_CLOUDY", cloud=88.77),
        ]

    s2_summary = _s2_summary("cesme", s2_query)
    assert s2_summary["en_guncel_postwindow_tarih"] == "2026-09-23"
    assert s2_summary["en_guncel_postwindow_global_bulut_yuzde"] == 88.77
    assert s2_summary["baseline_vs_en_guncel"]["hesaplanabilir"] is False
    assert s2_summary["baseline_vs_en_guncel"]["neden"] == "YUKSEK_GLOBAL_BULUT_YEREL_SCL_DOGRULAMASI_GEREKLI"
    assert s2_summary["en_guncel_uretim_filtresi_uygun_tarih"] == "2026-09-18"
    production_pair = s2_summary["baseline_vs_en_guncel_uretim_filtresi_uygun"]
    assert production_pair["hesaplanabilir"] is True
    assert production_pair["yeni_item"] == "S2_18"

    def s1_search(_bbox, days=30):
        del days
        return [_fake_s1("2026-09-11"), _fake_s1("2026-09-17"), _fake_s1("2026-09-18")]

    s1_summary = _s1_summary("cesme", s1_search)
    assert s1_summary["baseline_vs_en_guncel"]["en_guncel_postwindow_tarih"] == "2026-09-18"
    assert s1_summary["baseline_vs_en_guncel"]["hesaplanabilir"] is True
    assert satellite.MIN_HOTSPOT_AREA_M2 == 250
    print("critical onset latest scene audit self-check: ok")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check-only", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.self_check_only:
        _self_check()
        return
    if not args.live:
        raise SystemExit("Canlı metadata denetimi için --live kullanın.")
    report = build_report()
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    output = Path(args.output) if args.output else OUTPUT
    output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
