"""Sentinel-1 SAR sahne tazeligini yalniz diagnostik olarak izler.

Bu katman alarm veya saha gorevi uretmez. Amaci Sentinel-2 optik sahneleri
bulut/golge veya tekrar gecikmesi nedeniyle yeni zemin hareketini goremiyorken,
aynı operasyon kutularinda daha yeni, buluttan bagimsiz Sentinel-1 GRD
metadata kapsamasinin bulunup bulunmadigini olcmektir.

SAR degisimi tek basina kazi/santiye kaniti sayilmaz. Speckle, bakis geometrisi,
bitki/nem ve bina sacilimi gibi etkiler nedeniyle ancak ayni goreli yorunge +
ayni orbit yonu + ayni polarizasyon imzasina sahip sahneler ileride ayri bir
non-alarming diagnostik analizde karsilastirilabilir.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from satellite import REGIONS


EARTH_SEARCH_URL = "https://earth-search.aws.element84.com/v1/search"
S1_COLLECTION = "sentinel-1"
SEARCH_DAYS = 24
TIMEOUT_SECONDS = 35


def _iso_datetime(item):
    value = str((item.get("properties") or {}).get("datetime") or "")
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _relative_orbit(item):
    props = item.get("properties") or {}
    value = props.get("sat:relative_orbit")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _orbit_state(item):
    value = str((item.get("properties") or {}).get("sat:orbit_state") or "").lower()
    return value or None


def _polarizations(item):
    value = (item.get("properties") or {}).get("sar:polarizations")
    if isinstance(value, (list, tuple)):
        return tuple(sorted(str(v).upper() for v in value if v))
    if isinstance(value, str) and value:
        return tuple(sorted(part.strip().upper() for part in value.split(",") if part.strip()))
    return ()


def _instrument_mode(item):
    value = str((item.get("properties") or {}).get("sar:instrument_mode") or "").upper()
    return value or None


def _item_covers_bbox(item, bbox):
    footprint = item.get("bbox")
    if not isinstance(footprint, (list, tuple)) or len(footprint) < 4:
        return False
    try:
        west, south, east, north = map(float, bbox)
        iw, isouth, ie, inorth = map(float, footprint[:4])
    except (TypeError, ValueError):
        return False
    return iw <= west and isouth <= south and ie >= east and inorth >= north


def _compatible_signature(item):
    orbit = _relative_orbit(item)
    state = _orbit_state(item)
    pols = _polarizations(item)
    if orbit is None or not state or not pols:
        return None
    return orbit, state, pols


def _usable_item(item, bbox):
    dt = _iso_datetime(item)
    if dt is None or not _item_covers_bbox(item, bbox):
        return False
    mode = _instrument_mode(item)
    if mode and mode != "IW":
        return False
    return True


def _find_same_geometry_pair(items, bbox):
    usable = [item for item in items if _usable_item(item, bbox)]
    usable.sort(key=lambda row: _iso_datetime(row) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for i, latest in enumerate(usable):
        signature = _compatible_signature(latest)
        if signature is None:
            continue
        latest_dt = _iso_datetime(latest)
        for older in usable[i + 1 :]:
            if _compatible_signature(older) != signature:
                continue
            older_dt = _iso_datetime(older)
            if older_dt is None or latest_dt is None or older_dt >= latest_dt:
                continue
            return older, latest
    return None


def _search_items(bbox, days=SEARCH_DAYS):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    payload = {
        "collections": [S1_COLLECTION],
        "bbox": list(map(float, bbox)),
        "datetime": f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}",
        "limit": 80,
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    response = requests.post(EARTH_SEARCH_URL, json=payload, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json().get("features", [])


def _tracked_s2_date(region_key):
    path = Path("postseason_capacity_preview.json")
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        item = str(((payload.get("bolgeler") or {}).get(region_key) or {}).get("latest_item") or "")
    except (OSError, json.JSONDecodeError):
        return None
    match = re.search(r"(20\d{6})", item)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def inspect_region(region_key, search_fn=_search_items):
    bbox = (REGIONS.get(region_key) or {}).get("bbox")
    if not bbox:
        raise ValueError(f"Bilinmeyen bolge: {region_key}")

    try:
        items = search_fn(bbox)
    except requests.RequestException as exc:
        return {
            "bolge": region_key,
            "durum": "DIS_KAYNAK_GECICI_HATA",
            "hata": type(exc).__name__,
            "alarm": False,
            "saha_gorevi": False,
        }

    usable = [item for item in items if _usable_item(item, bbox)]
    usable.sort(key=lambda row: _iso_datetime(row) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    latest = usable[0] if usable else None
    pair = _find_same_geometry_pair(items, bbox)
    latest_dt = _iso_datetime(latest) if latest else None
    tracked_s2 = _tracked_s2_date(region_key)

    result = {
        "bolge": region_key,
        "durum": "OK" if latest else "TAM_KAPSAM_S1_BULUNAMADI",
        "aranan_gun": SEARCH_DAYS,
        "tam_kapsam_sahne_sayisi": len(usable),
        "son_s1_item": latest.get("id") if latest else None,
        "son_s1_tarih": latest_dt.date().isoformat() if latest_dt else None,
        "son_s1_goreli_yorunge": _relative_orbit(latest) if latest else None,
        "son_s1_orbit_yonu": _orbit_state(latest) if latest else None,
        "son_s1_polarizasyon": list(_polarizations(latest)) if latest else [],
        "ayni_geometri_cifti_var": pair is not None,
        "izlenen_s2_tarih": tracked_s2.isoformat() if tracked_s2 else None,
        "s1_s2den_daha_yeni": bool(latest_dt and tracked_s2 and latest_dt.date() > tracked_s2),
        "alarm": False,
        "saha_gorevi": False,
    }
    if pair:
        older, newer = pair
        result["karsilastirilabilir_s1_cifti"] = {
            "eski_item": older.get("id"),
            "eski_tarih": _iso_datetime(older).date().isoformat() if _iso_datetime(older) else None,
            "yeni_item": newer.get("id"),
            "yeni_tarih": _iso_datetime(newer).date().isoformat() if _iso_datetime(newer) else None,
            "goreli_yorunge": _relative_orbit(newer),
            "orbit_yonu": _orbit_state(newer),
            "polarizasyon": list(_polarizations(newer)),
        }
    return result


def _self_check():
    bbox = [26.45, 38.18, 26.68, 38.43]

    def fake(day, orbit=87, state="ascending", pols=("VV", "VH"), bbox_value=None, mode="IW"):
        return {
            "id": f"S1_{day}_{orbit}_{state}",
            "bbox": bbox_value or [26.0, 38.0, 27.0, 39.0],
            "properties": {
                "datetime": f"2026-09-{day:02d}T04:00:00Z",
                "sat:relative_orbit": orbit,
                "sat:orbit_state": state,
                "sar:polarizations": list(pols),
                "sar:instrument_mode": mode,
            },
        }

    items = [
        fake(11, orbit=87),
        fake(10, orbit=14),
        fake(5, orbit=87),
        fake(4, orbit=87, state="descending"),
        fake(3, orbit=87, bbox_value=[26.55, 38.25, 26.60, 38.30]),
    ]
    assert _usable_item(items[0], bbox)
    assert not _usable_item(items[-1], bbox)
    assert _compatible_signature(items[0]) == (87, "ascending", ("VH", "VV"))
    pair = _find_same_geometry_pair(items, bbox)
    assert pair is not None
    older, latest = pair
    assert older["id"].startswith("S1_5_87")
    assert latest["id"].startswith("S1_11_87")
    assert _find_same_geometry_pair([fake(11, 87), fake(5, 14)], bbox) is None
    assert _instrument_mode(fake(11, mode="EW")) == "EW"
    print("Sentinel-1 SAR kapsama diagnostigi oz testi OK.")


def _write_summary(rows):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## Sentinel-1 SAR kapsama diagnostigi",
        "",
        "Bu katman alarm veya saha gorevi uretmez; yalniz optik goruntu boslugunda buluttan bagimsiz gozlem tazeligini olcer.",
        "",
        "| Bolge | Son S1 | S2'den daha yeni | Ayni geometri cifti | Tam kapsam sahne |",
        "|---|---|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('bolge')} | {row.get('son_s1_tarih') or row.get('durum')} | "
            f"{'evet' if row.get('s1_s2den_daha_yeni') else 'hayir'} | "
            f"{'evet' if row.get('ayni_geometri_cifti_var') else 'hayir'} | "
            f"{row.get('tam_kapsam_sahne_sayisi', 0)} |"
        )
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check-only", action="store_true")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    if args.self_check_only:
        _self_check()
        return 0

    if not args.live:
        _self_check()
        return 0

    rows = [inspect_region("cesme"), inspect_region("uzunkuyu")]
    payload = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "kaynak": "Element 84 Earth Search sentinel-1 metadata",
        "diagnostik": rows,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    _write_summary(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
