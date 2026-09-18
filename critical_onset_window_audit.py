"""15-17 Eylül 2026 kazı başlangıç penceresindeki Sentinel veri gerçekliğini denetler.

Bu katman yalnız diagnostiktir: alarm veya saha görevi üretmez, Sentinel eşiklerini
gevşetmez ve olmayan bir tarih için görüntü varmış gibi davranmaz. Amaç 13→15,
15→16, 15→17 ve 16→17 istenen temporal farklarının hangilerinin gerçekten
hesaplanabilir olduğunu; ayrıca 15 Eylül öncesi sakin baseline ile 15-17 Eylül
müdahale penceresi arasında hangi optik/SAR karşılaştırmanın mevcut olduğunu açıkça
kaydetmektir.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

import requests

import satellite
import sentinel1_scene_probe as s1


WINDOW_DATES = tuple(date(2026, 9, day) for day in (13, 15, 16, 17))
INTERVENTION_DATES = {date(2026, 9, day) for day in (15, 16, 17)}
PAIR_REQUESTS = (
    (date(2026, 9, 13), date(2026, 9, 15)),
    (date(2026, 9, 15), date(2026, 9, 16)),
    (date(2026, 9, 15), date(2026, 9, 17)),
    (date(2026, 9, 16), date(2026, 9, 17)),
)
S2_START = "2026-09-12T00:00:00Z"
S2_END = "2026-09-18T23:59:59Z"
S2_LIMIT = 100
OUTPUT = Path(__file__).with_name("critical_onset_window_availability.json")
REGION_KEYS = ("cesme", "uzunkuyu")


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


def _query_s2(bbox, request_post=requests.post):
    payload = {
        "collections": ["sentinel-2-c1-l2a"],
        "bbox": list(map(float, bbox)),
        "datetime": f"{S2_START}/{S2_END}",
        "limit": S2_LIMIT,
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    response = request_post(satellite.EARTH_SEARCH_URL, json=payload, timeout=45)
    response.raise_for_status()
    features = response.json().get("features", [])
    usable = []
    for item in features:
        assets = item.get("assets") or {}
        if not all(key in assets for key in ("visual", "red", "nir", "scl")):
            continue
        if not satellite._item_covers_bbox(item, bbox):
            continue
        if _iso_date(item) not in WINDOW_DATES:
            continue
        usable.append(item)
    return usable


def _best_by_date(items):
    grouped = {}
    for item in items:
        day = _iso_date(item)
        if day not in WINDOW_DATES:
            continue
        old = grouped.get(day)
        if old is None or _cloud(item) < _cloud(old):
            grouped[day] = item
    return grouped


def _s2_pair_status(grouped, old_day, new_day):
    older = grouped.get(old_day)
    newer = grouped.get(new_day)
    if older is None or newer is None:
        missing = [
            day.isoformat()
            for day, item in ((old_day, older), (new_day, newer))
            if item is None
        ]
        return {
            "hesaplanabilir": False,
            "neden": "EKSIK_S2_TARIHI",
            "eksik_tarihler": missing,
        }
    same_tile = satellite._same_mgrs_tile(older, newer)
    old_orbit = satellite._relative_orbit(older)
    new_orbit = satellite._relative_orbit(newer)
    same_orbit = (
        old_orbit is not None and new_orbit is not None and old_orbit == new_orbit
    )
    return {
        "hesaplanabilir": bool(same_tile),
        "neden": "OK" if same_tile else "FARKLI_MGRS_KARO",
        "eski_item": older.get("id"),
        "yeni_item": newer.get("id"),
        "eski_bulut_yuzde": round(_cloud(older), 2),
        "yeni_bulut_yuzde": round(_cloud(newer), 2),
        "ayni_mgrs_karo": bool(same_tile),
        "ayni_goreli_yorunge": (
            same_orbit if old_orbit is not None and new_orbit is not None else None
        ),
    }


def _s2_summary(region_key, query_fn=_query_s2):
    bbox = satellite.REGIONS[region_key]["bbox"]
    try:
        items = query_fn(bbox)
    except Exception as exc:
        return {
            "durum": "DIS_KAYNAK_HATASI",
            "hata": type(exc).__name__,
            "mevcut_tarihler": [],
            "eksik_tarihler": [day.isoformat() for day in WINDOW_DATES],
            "farklar": {},
        }
    grouped = _best_by_date(items)
    pairs = {
        f"{old.isoformat()}->{new.isoformat()}": _s2_pair_status(
            grouped, old, new
        )
        for old, new in PAIR_REQUESTS
    }
    available = sorted(day.isoformat() for day in grouped)
    missing = [day.isoformat() for day in WINDOW_DATES if day not in grouped]
    intervention = sorted(day for day in grouped if day in INTERVENTION_DATES)
    baseline = grouped.get(date(2026, 9, 13))
    latest_intervention = grouped.get(intervention[-1]) if intervention else None
    baseline_pair = None
    if baseline is not None and latest_intervention is not None:
        baseline_pair = _s2_pair_status(
            grouped, date(2026, 9, 13), intervention[-1]
        )
    return {
        "durum": "ok",
        "mevcut_tarihler": available,
        "eksik_tarihler": missing,
        "farklar": pairs,
        "baseline_vs_mudahale": {
            "hesaplanabilir": bool(
                baseline_pair and baseline_pair.get("hesaplanabilir")
            ),
            "baseline_tarihi": "2026-09-13" if baseline is not None else None,
            "mudahale_tarihi": (
                intervention[-1].isoformat() if intervention else None
            ),
            "detay": baseline_pair,
        },
    }


def _s1_items(region_key, search_fn=s1._search_items):
    result = search_fn(s1.AOIS[region_key]["bbox"], days=30)
    if isinstance(result, dict):
        return (
            result.get("items") or [],
            result.get("kaynak"),
            result.get("kaynak_hatalari") or [],
        )
    return result or [], "test", []


def _s1_exact_pair(items, region_key, old_day, new_day):
    bbox = s1.AOIS[region_key]["bbox"]
    old_items = [
        item
        for item in items
        if _iso_date(item) == old_day and s1._usable_item(item, bbox)
    ]
    new_items = [
        item
        for item in items
        if _iso_date(item) == new_day and s1._usable_item(item, bbox)
    ]
    if not old_items or not new_items:
        missing = []
        if not old_items:
            missing.append(old_day.isoformat())
        if not new_items:
            missing.append(new_day.isoformat())
        return {
            "hesaplanabilir": False,
            "neden": "EKSIK_S1_TARIHI",
            "eksik_tarihler": missing,
        }
    for newer in new_items:
        signature = s1._compatible_signature(newer)
        new_points = set(s1._covered_points(newer, region_key))
        if signature is None or not new_points:
            continue
        for older in old_items:
            if s1._compatible_signature(older) != signature:
                continue
            common = sorted(
                new_points.intersection(s1._covered_points(older, region_key))
            )
            if not common:
                continue
            return {
                "hesaplanabilir": True,
                "neden": "OK_AYNI_GEOMETRI",
                "eski_item": older.get("id"),
                "yeni_item": newer.get("id"),
                "ortak_kritik_noktalar": common,
                "goreli_yorunge": s1._relative_orbit(newer),
                "orbit_yonu": s1._orbit_state(newer),
                "polarizasyon": list(s1._polarizations(newer)),
            }
    return {
        "hesaplanabilir": False,
        "neden": "AYNI_GEOMETRI_ORBIT_POLARIZASYON_YOK",
        "eksik_tarihler": [],
    }


def _s1_baseline_pair(items, region_key):
    baseline_days = sorted(
        {
            day
            for item in items
            if (day := _iso_date(item)) is not None and day < date(2026, 9, 15)
        },
        reverse=True,
    )
    intervention_days = sorted(
        {
            day
            for item in items
            if (day := _iso_date(item)) in INTERVENTION_DATES
        },
        reverse=True,
    )
    for new_day in intervention_days:
        for old_day in baseline_days:
            result = _s1_exact_pair(items, region_key, old_day, new_day)
            if result.get("hesaplanabilir"):
                result["baseline_tarihi"] = old_day.isoformat()
                result["mudahale_tarihi"] = new_day.isoformat()
                return result
    return {
        "hesaplanabilir": False,
        "neden": "KARSILASTIRILABILIR_BASELINE_MUDAHALE_S1_CIFTI_YOK",
    }


def _s1_summary(region_key, search_fn=s1._search_items):
    try:
        items, source, errors = _s1_items(region_key, search_fn)
    except Exception as exc:
        return {
            "durum": "DIS_KAYNAK_HATASI",
            "hata": type(exc).__name__,
            "mevcut_tarihler": [],
            "farklar": {},
        }
    relevant_days = sorted(
        {day for item in items if (day := _iso_date(item)) in WINDOW_DATES}
    )
    pairs = {
        f"{old.isoformat()}->{new.isoformat()}": _s1_exact_pair(
            items, region_key, old, new
        )
        for old, new in PAIR_REQUESTS
    }
    return {
        "durum": "ok",
        "metadata_kaynagi": source,
        "kaynak_hatalari": errors,
        "mevcut_tarihler": [day.isoformat() for day in relevant_days],
        "eksik_tarihler": [
            day.isoformat() for day in WINDOW_DATES if day not in relevant_days
        ],
        "farklar": pairs,
        "baseline_vs_mudahale": _s1_baseline_pair(items, region_key),
    }


def build_report(s2_query=_query_s2, s1_search=s1._search_items):
    regions = {}
    for region_key in REGION_KEYS:
        regions[region_key] = {
            "etiket": satellite.REGIONS[region_key]["label"],
            "sentinel2": _s2_summary(region_key, s2_query),
            "sentinel1": _s1_summary(region_key, s1_search),
        }
    return {
        "surum": 1,
        "olusturma_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "amac": (
            "15-17 Eylül 2026 kazı başlangıç penceresinde yalnız gerçekten mevcut "
            "Sentinel tarihleriyle temporal karşılaştırma yapılabilirliğini kaydetmek"
        ),
        "alarm": False,
        "saha_gorevi": False,
        "gercek_derinlik_olcumu": False,
        "ana_sentinel_esigi_m2": satellite.MIN_HOTSPOT_AREA_M2,
        "mikro_aralik_m2": [150, 249],
        "ozel_baslangic_penceresi": [
            "2026-09-15",
            "2026-09-16",
            "2026-09-17",
        ],
        "istenen_farklar": [
            f"{old.isoformat()}->{new.isoformat()}" for old, new in PAIR_REQUESTS
        ],
        "bolgeler": regions,
        "not": (
            "hesaplanabilir=false olan temporal fark için görüntü/fark üretilmez. "
            "Sentinel-2 10 m veriden derinlik ölçülmez; bu dosya yalnız veri "
            "tazeliği/karşılaştırılabilirlik diagnostigidir."
        ),
    }


def _fake_s2(day, item_id, cloud=5.0, tile="35SMC", orbit=8):
    bbox = satellite.REGIONS["cesme"]["bbox"]
    return {
        "id": item_id,
        "bbox": list(bbox),
        "properties": {
            "datetime": f"{day}T09:00:00Z",
            "eo:cloud_cover": cloud,
            "s2:mgrs_tile": tile,
            "sat:relative_orbit": orbit,
        },
        "assets": {key: {} for key in ("visual", "red", "nir", "scl")},
    }


def _fake_s1(day, orbit=87, state="ascending", pols=("VV", "VH")):
    bbox = s1.AOIS["cesme"]["bbox"]
    return {
        "id": f"S1_{day}_{orbit}",
        "bbox": list(bbox),
        "properties": {
            "datetime": f"{day}T06:00:00Z",
            "sat:relative_orbit": orbit,
            "sat:orbit_state": state,
            "sar:polarizations": list(pols),
            "sar:instrument_mode": "IW",
        },
    }


def _self_check():
    grouped = _best_by_date(
        [
            _fake_s2("2026-09-13", "S2_13", cloud=8),
            _fake_s2("2026-09-15", "S2_15", cloud=3),
        ]
    )
    assert _s2_pair_status(
        grouped, date(2026, 9, 13), date(2026, 9, 15)
    )["hesaplanabilir"] is True
    missing = _s2_pair_status(
        grouped, date(2026, 9, 15), date(2026, 9, 17)
    )
    assert missing["hesaplanabilir"] is False
    assert missing["eksik_tarihler"] == ["2026-09-17"]

    s1_items = [_fake_s1("2026-09-13"), _fake_s1("2026-09-15")]
    exact = _s1_exact_pair(
        s1_items, "cesme", date(2026, 9, 13), date(2026, 9, 15)
    )
    assert exact["hesaplanabilir"] is True, exact
    incompatible = [
        _fake_s1("2026-09-13", orbit=87),
        _fake_s1("2026-09-15", orbit=12),
    ]
    exact = _s1_exact_pair(
        incompatible, "cesme", date(2026, 9, 13), date(2026, 9, 15)
    )
    assert exact["hesaplanabilir"] is False
    assert exact["neden"] == "AYNI_GEOMETRI_ORBIT_POLARIZASYON_YOK", exact
    assert satellite.MIN_HOTSPOT_AREA_M2 == 250
    print("critical onset window audit self-check: ok")


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
