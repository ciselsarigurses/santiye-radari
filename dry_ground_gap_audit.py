"""Kuru/seyrek bitkili zeminde mevcut Sentinel filtresinin kaçırabileceği küçük değişimi ölçer.

Çeşme yaz sonunda çok kuru olduğu için yeni kazı her zaman belirgin bir NDVI kaybı
üretmeyebilir. Ana motor 250 m²+ alarm eşiğini, küçük-saha filtresini veya görev
listesini burada değiştirmiyoruz. Yalnız mevcut değişim maskesinin dışında kalan,
hem önce hem sonra düşük NDVI taşıyan ve Bare Soil Index (BSI) ile gerçek renk
farkında eşzamanlı spektral değişim gösteren 250-2.000 m² kümeleri sayıyoruz.

Bu dosyanın çıktısı yalnız diagnostiktir. Saha doğrulaması olmadan bu adaylar alarma
veya saha görevine dönüştürülmez. Amaç özellikle kuru tarla/toprak üzerinde başlayan
hafriyatların sistematik bir kör alan oluşturup oluşturmadığını ölçmektir.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np

import satellite
from daily_report import ISTANBUL, REPORT_REGIONS
from scanner import connect


AUDIT_FILE = Path(__file__).with_name("dry_ground_gap_audit.json")
MAX_AUDIT_AREA_M2 = 2_000
LOW_NDVI_MAX = 0.35
MAX_NDVI_CHANGE = 0.12
MIN_RGB_DIFFERENCE = 0.10
MAX_RGB_DIFFERENCE = 0.24
MIN_ABS_BRIGHTNESS_CHANGE = 0.025
MIN_ABS_BSI_CHANGE = 0.10
EXAMPLE_LIMIT = 8


def _bsi(blue, red, nir, swir):
    numerator = (swir + red) - (nir + blue)
    denominator = (swir + red) + (nir + blue)
    return np.divide(
        numerator,
        denominator,
        out=np.zeros_like(red, dtype="float32"),
        where=np.abs(denominator) > 0.001,
    )


def _component_location(component, bbox, shape):
    pixels = np.asarray(component, dtype="int32")
    centroid = pixels.mean(axis=0)
    distance = np.sum((pixels - centroid) ** 2, axis=1)
    representative = pixels[int(np.argmin(distance))]
    row, column = int(representative[0]), int(representative[1])
    height, width = shape
    west, south, east, north = bbox
    latitude = north - (row + 0.5) / height * (north - south)
    longitude = west + (column + 0.5) / width * (east - west)
    return round(latitude, 6), round(longitude, 6)


def _records(mask, bbox, pixel_area_m2, bsi_delta, rgb_difference):
    records = []
    for component in satellite._connected_components(mask):
        area_m2 = len(component) * pixel_area_m2
        if not (satellite.MIN_HOTSPOT_AREA_M2 <= area_m2 <= MAX_AUDIT_AREA_M2):
            continue
        pixels = np.asarray(component, dtype="int32")
        latitude, longitude = _component_location(component, bbox, mask.shape)
        records.append(
            {
                "mahalle": satellite._nearest_place(latitude, longitude),
                "enlem": latitude,
                "boylam": longitude,
                "alan_m2": round(area_m2),
                "ortalama_bsi_degisim": round(
                    float(np.mean(bsi_delta[pixels[:, 0], pixels[:, 1]])), 4
                ),
                "ortalama_rgb_farki": round(
                    float(np.mean(rgb_difference[pixels[:, 0], pixels[:, 1]])), 4
                ),
            }
        )
    return sorted(
        records,
        key=lambda item: (
            -float(item.get("ortalama_bsi_degisim") or 0),
            -float(item.get("ortalama_rgb_farki") or 0),
            float(item.get("alan_m2") or 0),
        ),
    )


def _analyze_region(region_key, pair):
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

    older_bsi = _bsi(older_blue, older_red, older_nir, older_swir)
    latest_bsi = _bsi(latest_blue, latest_red, latest_nir, latest_swir)
    bsi_delta = np.abs(latest_bsi - older_bsi)
    ndvi_delta = np.abs(latest_ndvi - older_ndvi)

    diagnostic_raw = (
        valid
        & ~production_mask
        & (older_ndvi < LOW_NDVI_MAX)
        & (latest_ndvi < LOW_NDVI_MAX)
        & (ndvi_delta < MAX_NDVI_CHANGE)
        & (rgb_difference > MIN_RGB_DIFFERENCE)
        & (rgb_difference < MAX_RGB_DIFFERENCE)
        & (np.abs(brightness_gain) > MIN_ABS_BRIGHTNESS_CHANGE)
        & (bsi_delta > MIN_ABS_BSI_CHANGE)
    )
    diagnostic_mask = satellite._retain_components(
        diagnostic_raw,
        satellite.SMALL_HOTSPOT_MIN_PIXELS,
    )

    west, south, east, north = bbox
    pixel_width_m = (
        (east - west)
        * 111320
        * np.cos(np.radians((south + north) / 2))
        / width
    )
    pixel_height_m = (north - south) * 110570 / height
    pixel_area_m2 = pixel_width_m * pixel_height_m
    records = _records(
        diagnostic_mask,
        bbox,
        pixel_area_m2,
        bsi_delta,
        rgb_difference,
    )
    return {
        "bolge": region["label"],
        "onceki_item": older.get("id"),
        "son_item": latest.get("id"),
        "onceki_tarih": satellite._item_date(older),
        "son_tarih": satellite._item_date(latest),
        "durum": "ok",
        "potansiyel_kuru_zemin_korlugu": len(records),
        "ornekler": records[:EXAMPLE_LIMIT],
    }


def _today_snapshot(connection, report_date, region_key):
    row = connection.execute(
        """SELECT son_item,hata FROM gunluk_uydu_raporlari
        WHERE rapor_tarihi=? AND bolge=? LIMIT 1""",
        (report_date, region_key),
    ).fetchone()
    if not row:
        return None, "rapor_yok"
    return row[0], row[1]


def _self_check():
    blue = np.array([[0.10]], dtype="float32")
    red = np.array([[0.20]], dtype="float32")
    nir = np.array([[0.15]], dtype="float32")
    swir = np.array([[0.35]], dtype="float32")
    value = float(_bsi(blue, red, nir, swir)[0, 0])
    assert -1.0 <= value <= 1.0
    assert value > 0, "Kuru/toprak örneğinde BSI pozitif bekleniyor."

    mask = np.zeros((5, 5), dtype=bool)
    mask[1, 1:4] = True
    retained = satellite._retain_components(mask, satellite.SMALL_HOTSPOT_MIN_PIXELS)
    assert int(retained.sum()) == 3
    weak = np.zeros((5, 5), dtype=bool)
    weak[1, 1:3] = True
    assert int(satellite._retain_components(
        weak, satellite.SMALL_HOTSPOT_MIN_PIXELS
    ).sum()) == 0


def run_audit():
    _self_check()
    report_date = datetime.now(ISTANBUL).strftime("%Y-%m-%d")
    payload = {
        "rapor_tarihi": report_date,
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "amac": (
            "Mevcut alarm üretim maskesinin dışında kalan düşük-NDVI 250-2.000 m² "
            "kuru zemin spektral değişimlerini BSI ile ölçmek; alarm/görev üretmez."
        ),
        "esikler": {
            "alan_m2": [satellite.MIN_HOTSPOT_AREA_M2, MAX_AUDIT_AREA_M2],
            "dusuk_ndvi_max": LOW_NDVI_MAX,
            "max_ndvi_degisim": MAX_NDVI_CHANGE,
            "rgb_farki": [MIN_RGB_DIFFERENCE, MAX_RGB_DIFFERENCE],
            "min_mutlak_parlaklik_degisim": MIN_ABS_BRIGHTNESS_CHANGE,
            "min_mutlak_bsi_degisim": MIN_ABS_BSI_CHANGE,
        },
        "bolgeler": {},
    }

    with connect() as connection:
        for region_key in REPORT_REGIONS:
            latest_item, report_error = _today_snapshot(
                connection, report_date, region_key
            )
            if report_error:
                payload["bolgeler"][region_key] = {
                    "bolge": satellite.REGIONS[region_key]["label"],
                    "durum": "atlandi",
                    "neden": str(report_error),
                }
                continue
            try:
                pair = satellite.sentinel_pair(region_key)
                live_latest = str(pair[1].get("id") or "")
                if str(latest_item or "") != live_latest:
                    payload["bolgeler"][region_key] = {
                        "bolge": satellite.REGIONS[region_key]["label"],
                        "durum": "atlandi",
                        "neden": "tarama_sirasinda_yeni_sahne_yayimlandi",
                        "rapor_sahnesi": latest_item,
                        "canli_sahne": live_latest,
                    }
                    continue
                payload["bolgeler"][region_key] = _analyze_region(region_key, pair)
            except Exception as exc:  # Diagnostik katman ana taramayı kırmamalı.
                payload["bolgeler"][region_key] = {
                    "bolge": satellite.REGIONS[region_key]["label"],
                    "durum": "hata",
                    "neden": str(exc),
                }

    AUDIT_FILE.write_text(
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
        print("Kuru zemin körlük denetimi öz testi başarılı; üretim alarmı değişmedi.")
        return

    payload = run_audit()
    counts = []
    for region_key, data in payload.get("bolgeler", {}).items():
        if data.get("durum") == "ok":
            counts.append(
                f"{region_key}={int(data.get('potansiyel_kuru_zemin_korlugu') or 0)}"
            )
        else:
            counts.append(f"{region_key}={data.get('durum')}")
    print(
        "Kuru zemin körlük denetimi tamamlandı: "
        + (", ".join(counts) or "bölge yok")
        + ". Bu kayıtlar alarm/görev değildir."
    )


if __name__ == "__main__":
    main()
