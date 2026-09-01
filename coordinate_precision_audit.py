"""Sentinel saha adaylarının 10 m sınıfı koordinat temsilini diagnostik olarak ölçer.

Üretim alarmı, görev sayısı veya eşikleri değiştirmez. ``dry_ground_temporal_pool.json``
içindeki kuru-zemin saha-benzeri adayları aynı Sentinel çifti ve aynı diagnostik maske
üzerinde tekrar bulur; mevcut geometrik temsil pikseli ile BSI×RGB değişim gücüne göre
hesaplanan sinyal-ağırlıklı temsil pikseli arasındaki metre farkını raporlar.

Amaç koordinatı körlemesine değiştirmek değil, saha ekibinin gideceği noktanın gerçekten
10 m sınıfında iyileştirilebileceğine dair önce ölçülebilir kanıt üretmektir. Sinyal-ağırlıklı
nokta yalnız aynı bağlı bileşenin içindeki gerçek Sentinel pikselinden seçilir.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

import dry_ground_gap_audit as gap
import satellite
from daily_report import ISTANBUL


SOURCE_POOL = Path(__file__).with_name("dry_ground_temporal_pool.json")
OUTPUT_AUDIT = Path(__file__).with_name("coordinate_precision_audit.json")


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _pixel_location(row, column, bbox, shape):
    height, width = shape
    west, south, east, north = bbox
    latitude = north - (int(row) + 0.5) / height * (north - south)
    longitude = west + (int(column) + 0.5) / width * (east - west)
    return round(latitude, 6), round(longitude, 6)


def _point_pixel(latitude, longitude, bbox, shape):
    height, width = shape
    west, south, east, north = bbox
    row = int((north - float(latitude)) / (north - south) * height)
    column = int((float(longitude) - west) / (east - west) * width)
    row = min(max(row, 0), height - 1)
    column = min(max(column, 0), width - 1)
    return row, column


def _representative_pixel(component):
    pixels = np.asarray(component, dtype="int32")
    centroid = pixels.mean(axis=0)
    distance = np.sum((pixels - centroid) ** 2, axis=1)
    representative = pixels[int(np.argmin(distance))]
    return int(representative[0]), int(representative[1])


def _weighted_representative_pixel(component, score):
    """Aynı bileşende sinyal ağırlıklı merkeze en yakın gerçek pikseli seç."""
    pixels = np.asarray(component, dtype="int32")
    values = score[pixels[:, 0], pixels[:, 1]].astype("float64")
    values = np.where(np.isfinite(values) & (values > 0), values, 0.0)
    total = float(values.sum())
    if total <= 0:
        return _representative_pixel(component)
    weighted = np.average(pixels.astype("float64"), axis=0, weights=values)
    distance = np.sum((pixels - weighted) ** 2, axis=1)
    representative = pixels[int(np.argmin(distance))]
    return int(representative[0]), int(representative[1])


def _diagnostic_components(region_key, pair):
    region = satellite.REGIONS[region_key]
    bbox = region["bbox"]
    older, latest = pair
    height, width = satellite._output_shape(bbox)

    older_visual = satellite._read_asset(
        older, "visual", bbox, height, width, "bilinear"
    )[:3]
    latest_visual = satellite._read_asset(
        latest, "visual", bbox, height, width, "bilinear"
    )[:3]
    older_red = satellite._reflectance(
        satellite._read_asset(older, "red", bbox, height, width, "bilinear")[0]
    )
    latest_red = satellite._reflectance(
        satellite._read_asset(latest, "red", bbox, height, width, "bilinear")[0]
    )
    older_nir = satellite._reflectance(
        satellite._read_asset(older, "nir", bbox, height, width, "bilinear")[0]
    )
    latest_nir = satellite._reflectance(
        satellite._read_asset(latest, "nir", bbox, height, width, "bilinear")[0]
    )
    older_blue = satellite._reflectance(
        satellite._read_asset(older, "blue", bbox, height, width, "bilinear")[0]
    )
    latest_blue = satellite._reflectance(
        satellite._read_asset(latest, "blue", bbox, height, width, "bilinear")[0]
    )
    older_swir = satellite._reflectance(
        satellite._read_asset(older, "swir16", bbox, height, width, "bilinear")[0]
    )
    latest_swir = satellite._reflectance(
        satellite._read_asset(latest, "swir16", bbox, height, width, "bilinear")[0]
    )
    older_scl = satellite._read_asset(
        older, "scl", bbox, height, width, "nearest"
    )[0]
    latest_scl = satellite._read_asset(
        latest, "scl", bbox, height, width, "nearest"
    )[0]

    valid = ~np.isin(older_scl, satellite.EXCLUDED_SCL_CLASSES)
    valid &= ~np.isin(latest_scl, satellite.EXCLUDED_SCL_CLASSES)

    old_rgb = np.moveaxis(older_visual, 0, 2).astype("float32") / 255
    new_rgb = np.moveaxis(latest_visual, 0, 2).astype("float32") / 255
    rgb_difference = np.mean(np.abs(new_rgb - old_rgb), axis=2)
    brightness_gain = np.mean(new_rgb, axis=2) - np.mean(old_rgb, axis=2)
    older_ndvi = satellite._ndvi(older_red, older_nir)
    latest_ndvi = satellite._ndvi(latest_red, latest_nir)

    soil_signal = (
        valid
        & (older_ndvi - latest_ndvi > 0.14)
        & (brightness_gain > 0.035)
        & (rgb_difference > 0.10)
    )
    strong_visual_change = valid & (rgb_difference > 0.24)
    small_site_signal = (
        valid
        & (older_ndvi - latest_ndvi > 0.20)
        & (latest_ndvi < 0.30)
        & (brightness_gain > 0.055)
        & (rgb_difference > 0.14)
    )
    production_mask = satellite._clean_mask(
        soil_signal | strong_visual_change,
        small_site_mask=small_site_signal,
    )

    older_bsi = gap._bsi(older_blue, older_red, older_nir, older_swir)
    latest_bsi = gap._bsi(latest_blue, latest_red, latest_nir, latest_swir)
    bsi_delta = np.abs(latest_bsi - older_bsi)
    ndvi_delta = np.abs(latest_ndvi - older_ndvi)

    diagnostic_raw = (
        valid
        & ~production_mask
        & (older_ndvi < gap.LOW_NDVI_MAX)
        & (latest_ndvi < gap.LOW_NDVI_MAX)
        & (ndvi_delta < gap.MAX_NDVI_CHANGE)
        & (rgb_difference > gap.MIN_RGB_DIFFERENCE)
        & (rgb_difference < gap.MAX_RGB_DIFFERENCE)
        & (np.abs(brightness_gain) > gap.MIN_ABS_BRIGHTNESS_CHANGE)
        & (bsi_delta > gap.MIN_ABS_BSI_CHANGE)
    )
    diagnostic_mask = satellite._retain_components(
        diagnostic_raw,
        satellite.SMALL_HOTSPOT_MIN_PIXELS,
    )
    components = satellite._connected_components(diagnostic_mask)
    score = bsi_delta * rgb_difference
    return bbox, diagnostic_mask.shape, components, score


def _component_index(components):
    lookup = {}
    for index, component in enumerate(components):
        for row, column in component:
            lookup[(int(row), int(column))] = index
    return lookup


def _analyze_region(region_key, region_data):
    bbox = satellite.REGIONS[region_key]["bbox"]
    pair = satellite.sentinel_pair(region_key)
    expected_old = str(region_data.get("onceki_item") or "")
    expected_new = str(region_data.get("son_item") or "")
    if str(pair[0].get("id") or "") != expected_old or str(pair[1].get("id") or "") != expected_new:
        return {
            "durum": "atlandi",
            "neden": "canli_sentinel_cifti_havuzla_eslesmiyor",
            "havuz_onceki": expected_old,
            "havuz_son": expected_new,
            "canli_onceki": pair[0].get("id"),
            "canli_son": pair[1].get("id"),
        }

    bbox, shape, components, score = _diagnostic_components(region_key, pair)
    lookup = _component_index(components)
    rows = []
    unmatched = 0
    seen = set()

    for raw in region_data.get("saha_benzeri_ornekler") or []:
        if not isinstance(raw, dict):
            continue
        try:
            latitude = float(raw.get("enlem"))
            longitude = float(raw.get("boylam"))
        except (TypeError, ValueError):
            continue
        point_key = (round(latitude, 6), round(longitude, 6))
        if point_key in seen:
            continue
        seen.add(point_key)

        pixel = _point_pixel(latitude, longitude, bbox, shape)
        component_id = lookup.get(pixel)
        if component_id is None:
            unmatched += 1
            continue
        component = components[component_id]
        geometric_pixel = _representative_pixel(component)
        weighted_pixel = _weighted_representative_pixel(component, score)
        geometric_point = _pixel_location(*geometric_pixel, bbox, shape)
        weighted_point = _pixel_location(*weighted_pixel, bbox, shape)
        shift_m = _distance_m(geometric_point, weighted_point)
        rows.append(
            {
                "mahalle": raw.get("mahalle"),
                "alan_m2": raw.get("alan_m2"),
                "mevcut_enlem": round(latitude, 6),
                "mevcut_boylam": round(longitude, 6),
                "geometrik_enlem": geometric_point[0],
                "geometrik_boylam": geometric_point[1],
                "sinyal_agirlikli_enlem": weighted_point[0],
                "sinyal_agirlikli_boylam": weighted_point[1],
                "geometrik_sinyal_kaymasi_m": round(shift_m, 1),
                "bilesen_piksel": len(component),
            }
        )

    rows.sort(key=lambda item: -float(item.get("geometrik_sinyal_kaymasi_m") or 0))
    return {
        "durum": "ok",
        "bolge": satellite.REGIONS[region_key]["label"],
        "onceki_item": pair[0].get("id"),
        "son_item": pair[1].get("id"),
        "olculen_aday": len(rows),
        "eslesmeyen_aday": unmatched,
        "10m_ustu_kayma": sum(
            1 for item in rows if float(item.get("geometrik_sinyal_kaymasi_m") or 0) > 10
        ),
        "20m_ustu_kayma": sum(
            1 for item in rows if float(item.get("geometrik_sinyal_kaymasi_m") or 0) > 20
        ),
        "maksimum_kayma_m": max(
            (float(item.get("geometrik_sinyal_kaymasi_m") or 0) for item in rows),
            default=0.0,
        ),
        "adaylar": rows,
    }


def _self_check():
    component = [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3)]
    score = np.zeros((5, 5), dtype="float32")
    score[1, 3] = 10.0
    score[2, 3] = 8.0
    geometric = _representative_pixel(component)
    weighted = _weighted_representative_pixel(component, score)
    assert geometric in component
    assert weighted in component
    assert weighted[1] == 3, (geometric, weighted)
    fallback = _weighted_representative_pixel(component, np.zeros((5, 5), dtype="float32"))
    assert fallback == geometric
    assert _distance_m((38.3, 26.3), (38.3, 26.3)) == 0


def run_audit():
    _self_check()
    if not SOURCE_POOL.exists():
        raise RuntimeError("dry_ground_temporal_pool.json bulunamadı")
    source = json.loads(SOURCE_POOL.read_text(encoding="utf-8"))
    payload = {
        "rapor_tarihi": source.get("rapor_tarihi"),
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "amac": (
            "Mevcut Sentinel aday koordinatının geometrik temsil pikseli ile aynı bağlı "
            "bileşendeki BSI×RGB değişim gücüne göre sinyal-ağırlıklı temsil pikseli "
            "arasındaki farkı ölçmek; koordinatı otomatik değiştirmeden 10 m sınıfı "
            "hassasiyet iyileştirmesini doğrulamak."
        ),
        "uyari": (
            "Bu çıktı alarm/görev/eşik değiştirmez. Sinyal-ağırlıklı koordinat yalnız "
            "diagnostiktir; saha doğrulaması olmadan kesin adres veya parsel değildir."
        ),
        "bolgeler": {},
    }
    for region_key, region_data in (source.get("bolgeler") or {}).items():
        if region_key not in ("cesme", "uzunkuyu"):
            continue
        if not isinstance(region_data, dict) or region_data.get("durum") != "ok":
            payload["bolgeler"][region_key] = {
                "durum": "atlandi",
                "neden": "temporal_havuz_bolgesi_hazir_degil",
            }
            continue
        try:
            payload["bolgeler"][region_key] = _analyze_region(region_key, region_data)
        except Exception as exc:
            payload["bolgeler"][region_key] = {
                "durum": "hata",
                "neden": f"{type(exc).__name__}: {exc}",
            }

    OUTPUT_AUDIT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Koordinat hassasiyeti öz testi başarılı; üretim alarmı değişmedi.")
        return
    payload = run_audit()
    parts = []
    for region_key, data in payload.get("bolgeler", {}).items():
        if data.get("durum") == "ok":
            parts.append(
                f"{region_key}={int(data.get('olculen_aday') or 0)} "
                f"(>10m={int(data.get('10m_ustu_kayma') or 0)}, "
                f">20m={int(data.get('20m_ustu_kayma') or 0)}, "
                f"max={float(data.get('maksimum_kayma_m') or 0):.1f}m)"
            )
        else:
            parts.append(f"{region_key}={data.get('durum')}")
    print("Koordinat hassasiyeti denetimi tamamlandı: " + ", ".join(parts))


if __name__ == "__main__":
    main()
