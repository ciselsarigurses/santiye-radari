"""Doğrulanmış kazı ve yanlış-pozitif saha noktalarında ham Sentinel imzasını ölçer.

Yalnız diagnostiktir; alarm, görev veya rota üretmez. Amaç mevcut parlak-toprak
maskesinin gerçek temel kazısını neden kaçırıp tarla/bahçe değişimini neden tuttuğunu
sayısal olarak görmek ve üretim filtresini körlemesine sertleştirmemektir.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

import satellite


OUTPUT_JSON = Path(__file__).with_name("excavation_reference_signature_review.json")
FEEDBACK_JSON = Path(__file__).with_name("manual_field_feedback.json")
REFERENCE_RESULTS = {"YANLIS_POZITIF", "DOGRULANMIS_KAZI"}


def _safe_mean(values):
    values = np.asarray(values)
    return float(np.mean(values)) if values.size else 0.0


def _safe_max(values):
    values = np.asarray(values)
    return float(np.max(values)) if values.size else 0.0


def _round(value, digits=4):
    return round(float(value), digits)


def _load_feedback():
    payload = json.loads(FEEDBACK_JSON.read_text(encoding="utf-8"))
    return [
        item for item in payload.get("kayitlar", [])
        if isinstance(item, dict)
        and str(item.get("sonuc") or "").upper() in REFERENCE_RESULTS
    ]


def _point_in_bbox(item, bbox):
    west, south, east, north = bbox
    lat = float(item["enlem"])
    lon = float(item["boylam"])
    return south <= lat <= north and west <= lon <= east


def _row_col(lat, lon, bbox, shape):
    height, width = shape
    west, south, east, north = bbox
    row = int((north - lat) / (north - south) * height)
    col = int((lon - west) / (east - west) * width)
    return (
        min(max(row, 0), height - 1),
        min(max(col, 0), width - 1),
    )


def _window(array, row, col, radius):
    r0 = max(row - radius, 0)
    r1 = min(row + radius + 1, array.shape[0])
    c0 = max(col - radius, 0)
    c1 = min(col + radius + 1, array.shape[1])
    return array[r0:r1, c0:c1]


def _region_arrays(region_key):
    bbox = satellite.REGIONS[region_key]["bbox"]
    older, latest = satellite.sentinel_pair(region_key)
    height, width = satellite._output_shape(bbox)

    older_visual = satellite._read_asset(older, "visual", bbox, height, width, "bilinear")[:3]
    latest_visual = satellite._read_asset(latest, "visual", bbox, height, width, "bilinear")[:3]
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
    older_scl = satellite._read_asset(older, "scl", bbox, height, width, "nearest")[0]
    latest_scl = satellite._read_asset(latest, "scl", bbox, height, width, "nearest")[0]

    valid = ~np.isin(older_scl, satellite.EXCLUDED_SCL_CLASSES)
    valid &= ~np.isin(latest_scl, satellite.EXCLUDED_SCL_CLASSES)
    water = (older_scl == 6) | (latest_scl == 6)
    valid &= ~satellite._dilate_mask(water, satellite.COASTAL_WATER_BUFFER_PIXELS)

    old_rgb = np.moveaxis(older_visual, 0, 2).astype("float32") / 255
    new_rgb = np.moveaxis(latest_visual, 0, 2).astype("float32") / 255
    old_brightness = np.mean(old_rgb, axis=2)
    new_brightness = np.mean(new_rgb, axis=2)
    brightness_gain = new_brightness - old_brightness
    rgb_difference = np.mean(np.abs(new_rgb - old_rgb), axis=2)
    older_ndvi = satellite._ndvi(older_red, older_nir)
    latest_ndvi = satellite._ndvi(latest_red, latest_nir)
    vegetation_loss = older_ndvi - latest_ndvi

    current_soil = (
        valid
        & (vegetation_loss > 0.14)
        & (brightness_gain > 0.035)
        & (rgb_difference > 0.10)
    )
    current_strong_visual = valid & (rgb_difference > 0.24)
    current_small = (
        valid
        & (vegetation_loss > 0.20)
        & (latest_ndvi < 0.30)
        & (brightness_gain > 0.055)
        & (rgb_difference > 0.14)
    )
    dark_subsoil = (
        valid
        & (latest_ndvi < 0.35)
        & (brightness_gain < -0.025)
        & (rgb_difference > 0.10)
    )

    return {
        "bbox": bbox,
        "shape": valid.shape,
        "older_date": satellite._item_date(older),
        "latest_date": satellite._item_date(latest),
        "older_item": older.get("id"),
        "latest_item": latest.get("id"),
        "valid": valid,
        "old_brightness": old_brightness,
        "new_brightness": new_brightness,
        "brightness_gain": brightness_gain,
        "rgb_difference": rgb_difference,
        "older_ndvi": older_ndvi,
        "latest_ndvi": latest_ndvi,
        "vegetation_loss": vegetation_loss,
        "current_soil": current_soil,
        "current_strong_visual": current_strong_visual,
        "current_small": current_small,
        "dark_subsoil": dark_subsoil,
    }


def _point_record(item, arrays):
    lat = float(item["enlem"])
    lon = float(item["boylam"])
    row, col = _row_col(lat, lon, arrays["bbox"], arrays["shape"])

    valid5 = _window(arrays["valid"], row, col, 2)
    valid9 = _window(arrays["valid"], row, col, 4)
    valid5_count = max(int(np.count_nonzero(valid5)), 1)
    valid9_count = max(int(np.count_nonzero(valid9)), 1)

    def vals(name, radius, valid_window):
        window = _window(arrays[name], row, col, radius)
        return window[valid_window]

    bg5 = vals("brightness_gain", 2, valid5)
    diff5 = vals("rgb_difference", 2, valid5)
    loss5 = vals("vegetation_loss", 2, valid5)
    latest_ndvi5 = vals("latest_ndvi", 2, valid5)
    soil5 = vals("current_soil", 2, valid5)
    strong5 = vals("current_strong_visual", 2, valid5)
    small5 = vals("current_small", 2, valid5)
    dark5 = vals("dark_subsoil", 2, valid5)

    bg9 = vals("brightness_gain", 4, valid9)
    diff9 = vals("rgb_difference", 4, valid9)
    dark9 = vals("dark_subsoil", 4, valid9)

    center_valid = bool(arrays["valid"][row, col])
    return {
        "id": item.get("id"),
        "sonuc": str(item.get("sonuc") or "").upper(),
        "enlem": round(lat, 6),
        "boylam": round(lon, 6),
        "piksel_satir": row,
        "piksel_sutun": col,
        "merkez_gecerli": center_valid,
        "merkez": {
            "onceki_ndvi": _round(arrays["older_ndvi"][row, col]),
            "son_ndvi": _round(arrays["latest_ndvi"][row, col]),
            "bitki_kaybi": _round(arrays["vegetation_loss"][row, col]),
            "parlaklik_degisim": _round(arrays["brightness_gain"][row, col]),
            "rgb_farki": _round(arrays["rgb_difference"][row, col]),
            "mevcut_soil_mask": bool(arrays["current_soil"][row, col]),
            "mevcut_strong_visual": bool(arrays["current_strong_visual"][row, col]),
            "mevcut_small_mask": bool(arrays["current_small"][row, col]),
            "koyu_alt_toprak_proxy": bool(arrays["dark_subsoil"][row, col]),
        },
        "cevre_5x5": {
            "gecerli_piksel": valid5_count,
            "ortalama_parlaklik_degisim": _round(_safe_mean(bg5)),
            "min_parlaklik_degisim": _round(np.min(bg5) if bg5.size else 0),
            "max_parlaklik_degisim": _round(np.max(bg5) if bg5.size else 0),
            "ortalama_rgb_farki": _round(_safe_mean(diff5)),
            "max_rgb_farki": _round(_safe_max(diff5)),
            "ortalama_bitki_kaybi": _round(_safe_mean(loss5)),
            "max_bitki_kaybi": _round(_safe_max(loss5)),
            "son_ndvi_ortalama": _round(_safe_mean(latest_ndvi5)),
            "kalici_bitki_orani": _round(np.count_nonzero(latest_ndvi5 > 0.35) / valid5_count),
            "ciplak_zemin_orani": _round(np.count_nonzero(latest_ndvi5 < 0.30) / valid5_count),
            "parlak_artis_orani": _round(np.count_nonzero(bg5 > 0.035) / valid5_count),
            "koyulasma_orani": _round(np.count_nonzero(bg5 < -0.025) / valid5_count),
            "soil_mask_orani": _round(np.count_nonzero(soil5) / valid5_count),
            "strong_visual_orani": _round(np.count_nonzero(strong5) / valid5_count),
            "small_mask_orani": _round(np.count_nonzero(small5) / valid5_count),
            "koyu_alt_toprak_proxy_orani": _round(np.count_nonzero(dark5) / valid5_count),
        },
        "cevre_9x9": {
            "gecerli_piksel": valid9_count,
            "ortalama_parlaklik_degisim": _round(_safe_mean(bg9)),
            "ortalama_rgb_farki": _round(_safe_mean(diff9)),
            "koyulasma_orani": _round(np.count_nonzero(bg9 < -0.025) / valid9_count),
            "koyu_alt_toprak_proxy_orani": _round(np.count_nonzero(dark9) / valid9_count),
        },
    }


def _self_check():
    bbox = [26.0, 38.0, 27.0, 39.0]
    assert _row_col(38.5, 26.5, bbox, (100, 100)) == (50, 50)
    array = np.arange(100).reshape(10, 10)
    assert _window(array, 0, 0, 2).shape == (3, 3)
    assert _window(array, 5, 5, 2).shape == (5, 5)


def audit():
    _self_check()
    feedback = _load_feedback()
    regions = {}
    for region_key in ("cesme", "uzunkuyu"):
        bbox = satellite.REGIONS[region_key]["bbox"]
        refs = [item for item in feedback if _point_in_bbox(item, bbox)]
        try:
            arrays = _region_arrays(region_key)
            regions[region_key] = {
                "bolge": satellite.REGIONS[region_key]["label"],
                "durum": "ok",
                "onceki_tarih": arrays["older_date"],
                "son_tarih": arrays["latest_date"],
                "older_item": arrays["older_item"],
                "latest_item": arrays["latest_item"],
                "referanslar": [_point_record(item, arrays) for item in refs],
            }
        except Exception as exc:
            regions[region_key] = {
                "bolge": satellite.REGIONS[region_key]["label"],
                "durum": "hata",
                "hata": f"{type(exc).__name__}: {exc}",
            }
    return {
        "surum": 1,
        "amac": "Saha doğrulamalarında gerçek kazı ile tarla/bahçe yanlış pozitiflerinin ham Sentinel imzasını karşılaştırmak",
        "alarm": False,
        "saha_gorevi": False,
        "gercek_derinlik_olcumu": False,
        "bolgeler": regions,
        "not": "Bu dosya yalnız kalibrasyon diagnostigidir; eşik veya rota kararı vermez."
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("excavation reference signature self-check: ok")
        return
    payload = audit()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
