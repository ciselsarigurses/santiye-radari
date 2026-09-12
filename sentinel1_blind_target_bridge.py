"""Sentinel-1 ayni-geometri ciftini optik kor alan hedefleriyle esler.

Bu katman yalniz diagnostiktir. Sentinel-1 geri-sacilim degisimi tek basina
insaat/kazi kaniti sayilmaz; alarm veya saha gorevi uretmez. Amac, Sentinel-2
bulut/golge nedeniyle goremedigi tarihsel-kara alanlardan hangilerinin guncel
ve karsilastirilabilir Sentinel-1 cifti tarafindan gercekten kapsandigini ve
raster karsilastirmasi icin gerekli polarizasyon assetlerinin bulunup
bulunmadigini olcmektir.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import sentinel1_scene_probe as s1


MIN_MAIN_M2 = 250
MICRO_RANGE_M2 = [150, 249]
MAX_TARGETS_PER_REGION = 8


def _safe_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _target(lat, lon, area, source, reason=None, locality=None):
    try:
        lat = float(lat)
        lon = float(lon)
        area = int(round(float(area)))
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or area < MIN_MAIN_M2:
        return None
    return {
        "enlem": round(lat, 6),
        "boylam": round(lon, 6),
        "alan_m2": area,
        "kaynak": source,
        "neden": reason or "OPTIK_KOR_ALAN",
        "mahalle_yaklasik": locality or "Mevki dogrulanmadi",
    }


def _load_targets():
    coverage = _safe_json("coverage_blind_area_audit.json")
    regions = coverage.get("bolgeler") or {}
    result = {"cesme": [], "uzunkuyu": [], "gulbahce": []}

    for region_key in ("cesme", "uzunkuyu"):
        rows = ((regions.get(region_key) or {}).get("ornekler") or [])
        parsed = []
        for row in rows:
            item = _target(
                row.get("enlem"),
                row.get("boylam"),
                row.get("alan_m2"),
                "coverage_blind_area_audit.json",
                row.get("neden"),
                row.get("mahalle_yaklasik"),
            )
            if item:
                parsed.append(item)
        parsed.sort(key=lambda row: row["alan_m2"], reverse=True)
        result[region_key] = parsed[:MAX_TARGETS_PER_REGION]

    gul = _safe_json("gulbahce_latest_state_blind_review.json")
    gul_rows = gul.get("guncel_sahne_kor_kumeleri") or []
    parsed_gul = []
    for row in gul_rows:
        item = _target(
            row.get("enlem"),
            row.get("boylam"),
            row.get("alan_m2"),
            "gulbahce_latest_state_blind_review.json",
            row.get("neden"),
            "Gulbahce",
        )
        if item:
            parsed_gul.append(item)
    parsed_gul.sort(key=lambda row: row["alan_m2"], reverse=True)
    result["gulbahce"] = parsed_gul[:MAX_TARGETS_PER_REGION]
    return result


def _asset_polarization_map(item):
    found = {}
    for key, asset in (item.get("assets") or {}).items():
        if not isinstance(asset, dict) or not asset.get("href"):
            continue
        text = " ".join(
            str(value or "")
            for value in (key, asset.get("title"), asset.get("description"))
        ).upper()
        for pol in ("VV", "VH", "HH", "HV"):
            if re.search(rf"(^|[^A-Z]){pol}([^A-Z]|$)", text):
                found.setdefault(pol, []).append(str(key))
    return {pol: sorted(set(keys)) for pol, keys in sorted(found.items())}


def _common_raster_polarizations(older, newer):
    old_map = _asset_polarization_map(older)
    new_map = _asset_polarization_map(newer)
    common = sorted(set(old_map).intersection(new_map))
    return common, old_map, new_map


def _target_covered_by_pair(target, older, newer):
    point = (target["enlem"], target["boylam"])
    return s1._point_in_item(older, point) and s1._point_in_item(newer, point)


def inspect_region(region_key, targets, search_fn=s1._search_items):
    aoi = s1.AOIS[region_key]
    search = search_fn(aoi["bbox"])
    if isinstance(search, dict):
        items = search.get("items") or []
        source = search.get("kaynak")
    else:
        items = search or []
        source = "test"

    pair = s1._find_same_geometry_pair(items, region_key)
    if not pair:
        return {
            "bolge": region_key,
            "durum": "KARSILASTIRILABILIR_S1_CIFTI_YOK",
            "metadata_kaynagi": source,
            "hedef_sayisi": len(targets),
            "cift_tarafindan_kapsanan_hedef": 0,
            "raster_karsilastirma_hazir": False,
            "alarm": False,
            "saha_gorevi": False,
        }

    older, newer = pair
    common_pols, old_assets, new_assets = _common_raster_polarizations(older, newer)
    pair_ready = bool(common_pols)

    checked = []
    for target in targets:
        covered = _target_covered_by_pair(target, older, newer)
        row = dict(target)
        row.update(
            {
                "s1_cifti_kapsiyor": bool(covered),
                "sar_raster_karsilastirmasina_uygun": bool(covered and pair_ready),
                "alarm": False,
                "saha_gorevi": False,
            }
        )
        checked.append(row)

    covered_count = sum(1 for row in checked if row["s1_cifti_kapsiyor"])
    ready_count = sum(1 for row in checked if row["sar_raster_karsilastirmasina_uygun"])
    return {
        "bolge": region_key,
        "durum": "HAZIR" if pair_ready else "CIFT_VAR_RASTER_ASSET_EKSIK",
        "metadata_kaynagi": source,
        "eski_item": older.get("id"),
        "eski_tarih": s1._iso_datetime(older).date().isoformat() if s1._iso_datetime(older) else None,
        "yeni_item": newer.get("id"),
        "yeni_tarih": s1._iso_datetime(newer).date().isoformat() if s1._iso_datetime(newer) else None,
        "goreli_yorunge": s1._relative_orbit(newer),
        "orbit_yonu": s1._orbit_state(newer),
        "ortak_metadata_polarizasyonlari": list(s1._polarizations(newer)),
        "ortak_raster_polarizasyonlari": common_pols,
        "eski_raster_assetleri": old_assets,
        "yeni_raster_assetleri": new_assets,
        "hedef_sayisi": len(checked),
        "cift_tarafindan_kapsanan_hedef": covered_count,
        "raster_karsilastirmaya_hazir_hedef": ready_count,
        "raster_karsilastirma_hazir": pair_ready,
        "hedefler": checked,
        "alarm": False,
        "saha_gorevi": False,
    }


def _self_check():
    bbox = s1.AOIS["gulbahce"]["bbox"]

    def fake(day, assets=True):
        payload = {
            "id": f"S1_{day}",
            "bbox": list(bbox),
            "properties": {
                "datetime": f"2026-09-{day:02d}T16:14:00Z",
                "sat:relative_orbit": 29,
                "sat:orbit_state": "ascending",
                "sar:polarizations": ["VV", "VH"],
                "sar:instrument_mode": "IW",
            },
        }
        if assets:
            payload["assets"] = {
                "vv": {"href": "https://example.test/vv.tif", "title": "VV"},
                "vh": {"href": "https://example.test/vh.tif", "title": "VH"},
            }
        return payload

    targets = [
        {
            "enlem": 38.341406,
            "boylam": 26.643308,
            "alan_m2": 1200,
            "kaynak": "test",
            "neden": "BULUT",
            "mahalle_yaklasik": "Gulbahce",
        }
    ]

    def fake_search(_bbox):
        assert list(_bbox) == list(bbox)
        return {"kaynak": "test", "items": [fake(11), fake(5)]}

    row = inspect_region("gulbahce", targets, search_fn=fake_search)
    assert row["durum"] == "HAZIR"
    assert row["ortak_raster_polarizasyonlari"] == ["VH", "VV"]
    assert row["cift_tarafindan_kapsanan_hedef"] == 1
    assert row["raster_karsilastirmaya_hazir_hedef"] == 1
    assert row["alarm"] is False and row["saha_gorevi"] is False

    def missing_assets(_bbox):
        return {"kaynak": "test", "items": [fake(11, assets=False), fake(5, assets=False)]}

    row = inspect_region("gulbahce", targets, search_fn=missing_assets)
    assert row["durum"] == "CIFT_VAR_RASTER_ASSET_EKSIK"
    assert row["raster_karsilastirmaya_hazir_hedef"] == 0
    print("Sentinel-1 optik kor alan hedef koprusu oz testi OK.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check-only", action="store_true")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    if args.self_check_only:
        _self_check()
        return

    targets = _load_targets()
    rows = []
    for region_key in ("cesme", "uzunkuyu", "gulbahce"):
        try:
            rows.append(inspect_region(region_key, targets.get(region_key) or []))
        except Exception as exc:  # dis kaynak/metadata hatasi diagnostigi durdurmasin
            rows.append(
                {
                    "bolge": region_key,
                    "durum": "DIAGNOSTIK_HATA",
                    "hata": type(exc).__name__,
                    "hedef_sayisi": len(targets.get(region_key) or []),
                    "alarm": False,
                    "saha_gorevi": False,
                }
            )

    payload = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": MIN_MAIN_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "amac": "Optik kor alanlari ayni-geometri Sentinel-1 ciftinin raster karsilastirma uygunluguyla eslemek; geri-sacilim degisimini henuz insaat/kazi kaniti saymamak.",
        "bolgeler": rows,
        "toplam_hedef": sum(int(row.get("hedef_sayisi") or 0) for row in rows),
        "sar_raster_karsilastirmaya_hazir_hedef": sum(int(row.get("raster_karsilastirmaya_hazir_hedef") or 0) for row in rows),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
