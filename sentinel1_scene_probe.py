"""Sentinel-1 SAR sahne tazeligini yalniz diagnostik olarak izler.

Bu katman alarm veya saha gorevi uretmez. Amaci Sentinel-2 optik sahneleri
bulut/golge veya tekrar gecikmesi nedeniyle yeni zemin hareketini goremiyorken,
operasyon alanlarinda daha yeni, buluttan bagimsiz Sentinel-1 GRD metadata
kapsamasinin bulunup bulunmadigini olcmektir.

Sentinel-1 urun granulleri genis uretim bbox'inin tamamini tek bir kayitta
kaplamak zorunda degildir. Bu nedenle kapsama, STAC sahnesinin hedef bbox ile
gercek kesisimi ve kritik operasyon noktalarini kapsayip kapsamadigi uzerinden
olculur. Bu yalniz kapsama diagnostigidir; SAR degisimi tek basina
kazi/santiye kaniti sayilmaz.

Metadata icin once anonim kullanima acik Microsoft Planetary Computer STAC
Sentinel-1 GRD katalogu kullanilir. Katalog gecici olarak erisilemez veya hic
sonuc donmezse Element 84 Earth Search sentinel-1 katalogu yedek kaynaktir.
Hicbir kaynak API anahtari, gizli anahtar veya ucretli servis gerektirmez.

Speckle, bakis geometrisi, bitki/nem ve bina sacilimi gibi etkiler nedeniyle
ileride SAR degisimi karsilastirilacaksa ancak ayni goreli yorunge + ayni orbit
yonu + ayni polarizasyon imzasina sahip ve en az bir ortak kritik operasyon
noktasini kapsayan sahneler eslestirilir.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from satellite import PLACE_CENTERS, REGIONS


PC_SEARCH_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
PC_S1_COLLECTION = "sentinel-1-grd"
EARTH_SEARCH_URL = "https://earth-search.aws.element84.com/v1/search"
EARTH_S1_COLLECTION = "sentinel-1"
SEARCH_DAYS = 24
TIMEOUT_SECONDS = 35

GULBAHCE_CENTER = PLACE_CENTERS["Gülbahçe"]
# 2 km operasyon + 150 m analiz baglami. Derece kutusu yalniz metadata sorgusu
# icindir; kesin idari/kadastral sinir degildir.
GULBAHCE_DIAGNOSTIC_BBOX = [26.6209, 38.3133, 26.6702, 38.3522]

AOIS = {
    "cesme": {
        "bbox": REGIONS["cesme"]["bbox"],
        "s2_region": "cesme",
        "kritik_noktalar": {
            name: PLACE_CENTERS[name]
            for name in ("Çeşme", "Alaçatı", "Ilıca", "Ovacık", "Çiftlikköy")
        },
    },
    "uzunkuyu": {
        "bbox": REGIONS["uzunkuyu"]["bbox"],
        "s2_region": "uzunkuyu",
        "kritik_noktalar": {
            name: PLACE_CENTERS[name]
            for name in ("Uzunkuyu", "Germiyan", "Ildır", "Gülbahçe")
        },
    },
    "gulbahce": {
        "bbox": GULBAHCE_DIAGNOSTIC_BBOX,
        "s2_region": "uzunkuyu",
        "kritik_noktalar": {"Gülbahçe": GULBAHCE_CENTER},
    },
}

S1_METADATA_SOURCES = (
    ("Microsoft Planetary Computer", PC_SEARCH_URL, PC_S1_COLLECTION),
    ("Element 84 Earth Search", EARTH_SEARCH_URL, EARTH_S1_COLLECTION),
)


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


def _bbox_values(item):
    footprint = item.get("bbox")
    if not isinstance(footprint, (list, tuple)) or len(footprint) < 4:
        return None
    try:
        return tuple(map(float, footprint[:4]))
    except (TypeError, ValueError):
        return None


def _bbox_overlap_fraction(item, target_bbox):
    footprint = _bbox_values(item)
    if footprint is None:
        return 0.0
    iw, isouth, ie, inorth = footprint
    west, south, east, north = map(float, target_bbox)
    width = max(0.0, min(ie, east) - max(iw, west))
    height = max(0.0, min(inorth, north) - max(isouth, south))
    target_area = max(0.0, east - west) * max(0.0, north - south)
    if target_area <= 0:
        return 0.0
    return max(0.0, min(1.0, (width * height) / target_area))


def _point_in_item(item, point):
    footprint = _bbox_values(item)
    if footprint is None:
        return False
    lat, lon = map(float, point)
    west, south, east, north = footprint
    return west <= lon <= east and south <= lat <= north


def _covered_points(item, region_key):
    points = (AOIS.get(region_key) or {}).get("kritik_noktalar") or {}
    return sorted(name for name, point in points.items() if _point_in_item(item, point))


def _compatible_signature(item):
    orbit = _relative_orbit(item)
    state = _orbit_state(item)
    pols = _polarizations(item)
    if orbit is None or not state or not pols:
        return None
    return orbit, state, pols


def _usable_item(item, bbox):
    dt = _iso_datetime(item)
    if dt is None or _bbox_overlap_fraction(item, bbox) <= 0:
        return False
    mode = _instrument_mode(item)
    if mode and mode != "IW":
        return False
    return True


def _find_same_geometry_pair(items, region_key):
    bbox = AOIS[region_key]["bbox"]
    usable = [item for item in items if _usable_item(item, bbox)]
    usable.sort(
        key=lambda row: _iso_datetime(row) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for i, latest in enumerate(usable):
        signature = _compatible_signature(latest)
        latest_points = set(_covered_points(latest, region_key))
        if signature is None or not latest_points:
            continue
        latest_dt = _iso_datetime(latest)
        for older in usable[i + 1 :]:
            if _compatible_signature(older) != signature:
                continue
            if not latest_points.intersection(_covered_points(older, region_key)):
                continue
            older_dt = _iso_datetime(older)
            if older_dt is None or latest_dt is None or older_dt >= latest_dt:
                continue
            return older, latest
    return None


def _query_source(url, collection, bbox, days):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    payload = {
        "collections": [collection],
        "bbox": list(map(float, bbox)),
        "datetime": f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}",
        "limit": 80,
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    response = requests.post(url, json=payload, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json().get("features", [])


def _search_items(bbox, days=SEARCH_DAYS):
    errors = []
    empty_sources = []
    for name, url, collection in S1_METADATA_SOURCES:
        try:
            items = _query_source(url, collection, bbox, days)
        except requests.RequestException as exc:
            errors.append({"kaynak": name, "hata": type(exc).__name__})
            continue
        if items:
            return {
                "kaynak": name,
                "collection": collection,
                "items": items,
                "kaynak_hatalari": errors,
                "bos_kaynaklar": empty_sources,
            }
        empty_sources.append(name)
    return {
        "kaynak": None,
        "collection": None,
        "items": [],
        "kaynak_hatalari": errors,
        "bos_kaynaklar": empty_sources,
    }


def _tracked_s2_date(region_key):
    path = Path("postseason_capacity_preview.json")
    if not path.exists():
        return None
    s2_region = str((AOIS.get(region_key) or {}).get("s2_region") or region_key)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        item = str(((payload.get("bolgeler") or {}).get(s2_region) or {}).get("latest_item") or "")
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
    aoi = AOIS.get(region_key)
    if not aoi:
        raise ValueError(f"Bilinmeyen bolge: {region_key}")
    bbox = aoi["bbox"]

    try:
        search_result = search_fn(bbox)
    except requests.RequestException as exc:
        return {
            "bolge": region_key,
            "durum": "DIS_KAYNAK_GECICI_HATA",
            "hata": type(exc).__name__,
            "alarm": False,
            "saha_gorevi": False,
        }

    if isinstance(search_result, dict) and "items" in search_result:
        items = search_result.get("items") or []
        source_name = search_result.get("kaynak")
        collection = search_result.get("collection")
        source_errors = search_result.get("kaynak_hatalari") or []
        empty_sources = search_result.get("bos_kaynaklar") or []
    else:
        items = search_result or []
        source_name = "test"
        collection = None
        source_errors = []
        empty_sources = []

    intersecting = [item for item in items if _usable_item(item, bbox)]
    intersecting.sort(
        key=lambda row: _iso_datetime(row) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    critical = [item for item in intersecting if _covered_points(item, region_key)]
    latest = critical[0] if critical else (intersecting[0] if intersecting else None)
    pair = _find_same_geometry_pair(items, region_key)
    latest_dt = _iso_datetime(latest) if latest else None
    tracked_s2 = _tracked_s2_date(region_key)
    covered = _covered_points(latest, region_key) if latest else []
    overlap = _bbox_overlap_fraction(latest, bbox) if latest else 0.0

    if critical:
        status = "OK_KRITIK_NOKTA_KAPSAMASI"
    elif intersecting:
        status = "YALNIZ_BBOX_KESISIMI"
    elif source_errors and not source_name:
        status = "S1_METADATA_KAYNAKLARI_ERISILEMEDI"
    else:
        status = "S1_KESISIMI_BULUNAMADI"

    result = {
        "bolge": region_key,
        "durum": status,
        "aranan_gun": SEARCH_DAYS,
        "metadata_kaynagi": source_name,
        "metadata_collection": collection,
        "bos_kaynaklar": empty_sources,
        "kaynak_hatalari": source_errors,
        "stac_ham_sahne_sayisi": len(items),
        "kesisen_sahne_sayisi": len(intersecting),
        "kritik_nokta_kapsayan_sahne_sayisi": len(critical),
        "son_s1_item": latest.get("id") if latest else None,
        "son_s1_tarih": latest_dt.date().isoformat() if latest_dt else None,
        "son_s1_hedef_bbox_ortusme_orani": round(overlap, 4),
        "son_s1_kapsanan_kritik_noktalar": covered,
        "son_s1_goreli_yorunge": _relative_orbit(latest) if latest else None,
        "son_s1_orbit_yonu": _orbit_state(latest) if latest else None,
        "son_s1_polarizasyon": list(_polarizations(latest)) if latest else [],
        "ayni_geometri_ortak_nokta_cifti_var": pair is not None,
        "izlenen_s2_tarih": tracked_s2.isoformat() if tracked_s2 else None,
        "s1_s2den_daha_yeni": bool(latest_dt and tracked_s2 and latest_dt.date() > tracked_s2),
        "alarm": False,
        "saha_gorevi": False,
    }
    if pair:
        older, newer = pair
        common_points = sorted(
            set(_covered_points(older, region_key)).intersection(
                _covered_points(newer, region_key)
            )
        )
        result["karsilastirilabilir_s1_cifti"] = {
            "eski_item": older.get("id"),
            "eski_tarih": _iso_datetime(older).date().isoformat() if _iso_datetime(older) else None,
            "yeni_item": newer.get("id"),
            "yeni_tarih": _iso_datetime(newer).date().isoformat() if _iso_datetime(newer) else None,
            "goreli_yorunge": _relative_orbit(newer),
            "orbit_yonu": _orbit_state(newer),
            "polarizasyon": list(_polarizations(newer)),
            "ortak_kritik_noktalar": common_points,
        }
    return result


def _self_check():
    region_key = "uzunkuyu"
    bbox = AOIS[region_key]["bbox"]

    def fake(day, orbit=87, state="ascending", pols=("VV", "VH"), bbox_value=None, mode="IW"):
        return {
            "id": f"S1_{day}_{orbit}_{state}",
            "bbox": bbox_value or [26.40, 38.10, 26.70, 38.50],
            "properties": {
                "datetime": f"2026-09-{day:02d}T04:00:00Z",
                "sat:relative_orbit": orbit,
                "sat:orbit_state": state,
                "sar:polarizations": list(pols),
                "sar:instrument_mode": mode,
            },
        }

    partial = fake(3, bbox_value=[26.62, 38.30, 26.67, 38.36])
    outside = fake(2, bbox_value=[26.80, 38.50, 26.90, 38.60])
    items = [
        fake(11, orbit=87),
        fake(10, orbit=14),
        fake(5, orbit=87),
        fake(4, orbit=87, state="descending"),
        partial,
        outside,
    ]
    assert _usable_item(items[0], bbox)
    assert _usable_item(partial, bbox)
    assert not _usable_item(outside, bbox)
    assert 0 < _bbox_overlap_fraction(partial, bbox) < 1
    assert "Gülbahçe" in _covered_points(partial, region_key)
    assert _compatible_signature(items[0]) == (87, "ascending", ("VH", "VV"))
    pair = _find_same_geometry_pair(items, region_key)
    assert pair is not None
    older, latest = pair
    assert older["id"].startswith("S1_5_87")
    assert latest["id"].startswith("S1_11_87")
    assert _find_same_geometry_pair([fake(11, 87), fake(5, 14)], region_key) is None
    assert _instrument_mode(fake(11, mode="EW")) == "EW"

    def fake_query(url, collection, query_bbox, days):
        assert query_bbox == bbox
        if "planetarycomputer" in url:
            return items[:2]
        return []

    original = globals()["_query_source"]
    try:
        globals()["_query_source"] = fake_query
        search = _search_items(bbox)
        assert search["kaynak"] == "Microsoft Planetary Computer"
        assert len(search["items"]) == 2
    finally:
        globals()["_query_source"] = original

    print("Sentinel-1 SAR acik STAC ve bolgesel kesisim diagnostigi oz testi OK.")


def _write_summary(rows):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## Sentinel-1 SAR kapsama diagnostigi",
        "",
        "Bu katman alarm veya saha gorevi uretmez; yalniz optik goruntu boslugunda buluttan bagimsiz gozlem tazeligini olcer.",
        "",
        "| Bolge | Kaynak | Son S1 | S2'den daha yeni | Kritik nokta | Ayni geometri cifti |",
        "|---|---|---|---:|---|---:|",
    ]
    for row in rows:
        covered = ", ".join(row.get("son_s1_kapsanan_kritik_noktalar") or []) or "-"
        lines.append(
            f"| {row.get('bolge')} | {row.get('metadata_kaynagi') or '-'} | "
            f"{row.get('son_s1_tarih') or row.get('durum')} | "
            f"{'evet' if row.get('s1_s2den_daha_yeni') else 'hayir'} | {covered} | "
            f"{'evet' if row.get('ayni_geometri_ortak_nokta_cifti_var') else 'hayir'} |"
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

    rows = [inspect_region("cesme"), inspect_region("uzunkuyu"), inspect_region("gulbahce")]
    payload = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "kaynak_politikasi": "Planetary Computer sentinel-1-grd; sonuc/erisim yoksa Element 84 sentinel-1 yedegi",
        "diagnostik": rows,
        "not": "SAR metadata yalniz kapsama/tazelik diagnostigidir; tek basina insaat, kazi veya saha gorevi uretmez.",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    _write_summary(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
