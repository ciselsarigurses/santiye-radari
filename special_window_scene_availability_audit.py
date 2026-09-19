"""15-17 Eylül 2026 özel başlangıç penceresinde exact Sentinel veri uygunluğunu ölçer.

Bu katman yalnız metadata diagnostiğidir. Görüntü yokken varmış gibi davranmayı engeller;
alarm, saha görevi, rota, 250 m² ana eşik veya 150-249 m² MİKRO politikasını değiştirmez.

Sentinel-2 için 13, 15, 16 ve 17 Eylül tarihlerinde tam AOI kapsayan sahneleri ve
13→15, 15→16, 15→17, 16→17 exact çiftlerinin aynı MGRS/tercihen aynı göreli yörüngeyle
kurulup kurulamadığını raporlar. Sentinel-1 için aynı tarihlerde AOI ile kesişen IW
sahnelerini ve exact çiftte aynı orbit imzası + ortak kritik nokta bulunup bulunmadığını
raporlar. Bu çıktı yalnız veri varlığı/geometrisi hakkındadır; kazı kanıtı değildir.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import satellite
import sentinel1_scene_probe as s1


OUTPUT = Path(__file__).with_name("special_window_scene_availability_review.json")
FOCUS_DATES = ("2026-09-13", "2026-09-15", "2026-09-16", "2026-09-17")
REQUESTED_PAIRS = (
    ("2026-09-13", "2026-09-15"),
    ("2026-09-15", "2026-09-16"),
    ("2026-09-15", "2026-09-17"),
    ("2026-09-16", "2026-09-17"),
)
REGION_KEYS = ("cesme", "uzunkuyu")
S2_BSI_ASSETS = ("blue", "red", "nir", "swir16", "scl")


def _date_text(item):
    raw = str((item.get("properties") or {}).get("datetime") or "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _cloud(item):
    try:
        return float((item.get("properties") or {}).get("eo:cloud_cover"))
    except (TypeError, ValueError):
        return None


def _s2_full_candidates(items, bbox, target_date, require_bsi=False):
    rows = []
    for item in items:
        if _date_text(item) != target_date:
            continue
        if not satellite._item_covers_bbox(item, bbox):
            continue
        if require_bsi and not all(key in (item.get("assets") or {}) for key in S2_BSI_ASSETS):
            continue
        rows.append(item)
    return rows


def _s2_pair_status(left, right):
    if not left or not right:
        return {
            "exact_cift_var": False,
            "ayni_mgrs_var": False,
            "ayni_goreli_yorunge_var": False,
            "durum": "VERI_YOK",
        }
    same_tile = []
    same_orbit = []
    for first in left:
        for second in right:
            if not satellite._same_mgrs_tile(first, second):
                continue
            same_tile.append((first, second))
            orbit_a = satellite._relative_orbit(first)
            orbit_b = satellite._relative_orbit(second)
            if orbit_a is not None and orbit_b is not None and orbit_a == orbit_b:
                same_orbit.append((first, second))
    if same_orbit:
        status = "EXACT_AYNI_YORUNGE"
    elif same_tile:
        status = "EXACT_FARKLI_YORUNGE_RISKI"
    else:
        status = "EXACT_GEOMETRI_ESLESMIYOR"
    return {
        "exact_cift_var": bool(same_tile),
        "ayni_mgrs_var": bool(same_tile),
        "ayni_goreli_yorunge_var": bool(same_orbit),
        "durum": status,
    }


def _s2_region(region_key):
    bbox = satellite.REGIONS[region_key]["bbox"]
    try:
        # Bulutlu sahneyi de 'veri yok' sanmamak için metadata havuzunda bulut eşiğini
        # kaldır. Aşağıda minimum bulut oranı ayrıca raporlanır; analiz uygunluğu değildir.
        items = satellite._search_items(bbox, days=30, max_cloud=100)
    except Exception as exc:
        return {"durum": "HATA", "hata": f"{type(exc).__name__}: {exc}"}

    dates = {}
    morph_by_date = {}
    bsi_by_date = {}
    for target in FOCUS_DATES:
        morph = _s2_full_candidates(items, bbox, target, require_bsi=False)
        bsi = _s2_full_candidates(items, bbox, target, require_bsi=True)
        morph_by_date[target] = morph
        bsi_by_date[target] = bsi
        clouds = [value for item in morph if (value := _cloud(item)) is not None]
        dates[target] = {
            "tam_kapsam_morfoloji_sahnesi": len(morph),
            "tam_kapsam_bsi_sahnesi": len(bsi),
            "minimum_bulut_yuzde": round(min(clouds), 3) if clouds else None,
            "itemler": [str(item.get("id") or "") for item in morph[:4]],
        }

    pairs = {}
    for start, end in REQUESTED_PAIRS:
        morph_status = _s2_pair_status(morph_by_date[start], morph_by_date[end])
        bsi_status = _s2_pair_status(bsi_by_date[start], bsi_by_date[end])
        pairs[f"{start}->{end}"] = {
            "morfoloji": morph_status,
            "bsi": bsi_status,
        }

    return {
        "durum": "ok",
        "bolge": satellite.REGIONS[region_key]["label"],
        "tarihler": dates,
        "exact_ciftler": pairs,
    }


def _s1_candidates(items, region_key, target_date):
    bbox = s1.AOIS[region_key]["bbox"]
    return [
        item for item in items
        if _date_text(item) == target_date and s1._usable_item(item, bbox)
    ]


def _s1_pair_status(first_items, second_items, region_key):
    if not first_items or not second_items:
        return {
            "exact_cift_var": False,
            "ayni_orbit_imzasi_var": False,
            "ortak_kritik_nokta_var": False,
            "durum": "VERI_YOK",
        }
    compatible = []
    for first in first_items:
        signature = s1._compatible_signature(first)
        first_points = set(s1._covered_points(first, region_key))
        if signature is None or not first_points:
            continue
        for second in second_items:
            if s1._compatible_signature(second) != signature:
                continue
            common = first_points.intersection(s1._covered_points(second, region_key))
            if common:
                compatible.append(sorted(common))
    return {
        "exact_cift_var": bool(compatible),
        "ayni_orbit_imzasi_var": bool(compatible),
        "ortak_kritik_nokta_var": bool(compatible),
        "ortak_kritik_noktalar": compatible[0] if compatible else [],
        "durum": "EXACT_UYUMLU" if compatible else "EXACT_GEOMETRI_ESLESMIYOR",
    }


def _s1_region(region_key):
    bbox = s1.AOIS[region_key]["bbox"]
    result = s1._search_items(bbox, days=30)
    items = list(result.get("items") or [])
    if not items:
        return {
            "durum": "HATA",
            "kaynak": result.get("kaynak"),
            "kaynak_hatalari": result.get("kaynak_hatalari") or [],
            "bos_kaynaklar": result.get("bos_kaynaklar") or [],
            "hata": "Sentinel-1 metadata sahnesi bulunamadı",
        }

    by_date = {}
    dates = {}
    for target in FOCUS_DATES:
        candidates = _s1_candidates(items, region_key, target)
        by_date[target] = candidates
        points = sorted({
            point
            for item in candidates
            for point in s1._covered_points(item, region_key)
        })
        dates[target] = {
            "aoi_kesisen_iw_sahnesi": len(candidates),
            "kapsanan_kritik_noktalar": points,
            "itemler": [str(item.get("id") or "") for item in candidates[:6]],
        }

    pairs = {
        f"{start}->{end}": _s1_pair_status(by_date[start], by_date[end], region_key)
        for start, end in REQUESTED_PAIRS
    }
    return {
        "durum": "ok",
        "bolge": satellite.REGIONS[region_key]["label"],
        "kaynak": result.get("kaynak"),
        "kaynak_hatalari": result.get("kaynak_hatalari") or [],
        "bos_kaynaklar": result.get("bos_kaynaklar") or [],
        "tarihler": dates,
        "exact_ciftler": pairs,
    }


def _synthetic_s2(day, orbit=42, tile="35SMC"):
    return {
        "id": f"S2-{day}-{orbit}",
        "bbox": [26.0, 38.0, 27.0, 39.0],
        "properties": {
            "datetime": f"{day}T09:00:00Z",
            "s2:mgrs_tile": tile,
            "sat:relative_orbit": orbit,
            "eo:cloud_cover": 5,
        },
        "assets": {key: {} for key in (*S2_BSI_ASSETS, "visual")},
    }


def _synthetic_s1(day, orbit=7):
    return {
        "id": f"S1-{day}-{orbit}",
        "bbox": [26.0, 38.0, 27.0, 39.0],
        "properties": {
            "datetime": f"{day}T03:00:00Z",
            "sat:relative_orbit": orbit,
            "sat:orbit_state": "ascending",
            "sar:polarizations": ["VV", "VH"],
            "sar:instrument_mode": "IW",
        },
    }


def _self_check():
    left = [_synthetic_s2("2026-09-13")]
    right = [_synthetic_s2("2026-09-15")]
    assert _s2_pair_status(left, right)["durum"] == "EXACT_AYNI_YORUNGE"
    assert _s2_pair_status(left, [_synthetic_s2("2026-09-15", orbit=99)])["durum"] == "EXACT_FARKLI_YORUNGE_RISKI"
    assert _s2_pair_status(left, [])["durum"] == "VERI_YOK"

    original = s1.AOIS["cesme"]["kritik_noktalar"]
    try:
        s1.AOIS["cesme"]["kritik_noktalar"] = {"TEST": (38.5, 26.5)}
        first = [_synthetic_s1("2026-09-13")]
        second = [_synthetic_s1("2026-09-15")]
        assert _s1_pair_status(first, second, "cesme")["durum"] == "EXACT_UYUMLU"
        assert _s1_pair_status(first, [_synthetic_s1("2026-09-15", orbit=8)], "cesme")["durum"] == "EXACT_GEOMETRI_ESLESMIYOR"
    finally:
        s1.AOIS["cesme"]["kritik_noktalar"] = original


def audit():
    _self_check()
    return {
        "surum": 1,
        "amac": "13/15/16/17 Eylül exact Sentinel-2 ve Sentinel-1 veri/geometri uygunluğunu açıkça doğrulamak",
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "tarihler": list(FOCUS_DATES),
        "istenen_exact_ciftler": [f"{start}->{end}" for start, end in REQUESTED_PAIRS],
        "sentinel2": {key: _s2_region(key) for key in REGION_KEYS},
        "sentinel1": {key: _s1_region(key) for key in REGION_KEYS},
        "not": (
            "VERI_YOK veya EXACT_GEOMETRI_ESLESMIYOR olan çift için görüntü varmış gibi temporal fark üretilmez. "
            "Metadata uygunluğu kazı/şantiye kanıtı değildir; yalnız hangi exact karşılaştırmanın fiziksel olarak kurulabileceğini gösterir."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("special window scene availability self-check: ok")
        return 0
    payload = audit()
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
