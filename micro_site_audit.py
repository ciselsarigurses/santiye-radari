"""150-249 m² için alarm-dışı Sentinel mikro-şantiye diagnostik katmanı.

Ana üretim eşiği 250 m² olarak kalır. Bu dosya yalnız mevcut 10 m sınıfı Sentinel
çözünürlüğünde iki bitişik güçlü piksele düşebilen yaklaşık 150-249 m² kompakt
zemin değişimlerini ölçer. Sonuçlar saha görevi/alarm üretmez; 09:30 raporunda ancak
ayrı temporal veya açık-web doğrulamasıyla güçlenirse değerlendirilebilecek bir
kalibrasyon havuzudur.

Gülbahçe ayrıca açık bir referans merkezle kapsama regresyonuna alınır. Referans
merkez mahalle sınırı değildir ve adres/parsel doğrulaması olarak kullanılmaz.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

import satellite


OUTPUT_FILE = Path(__file__).with_name("micro_site_audit.json")
ISTANBUL = ZoneInfo("Europe/Istanbul")
MICRO_MIN_AREA_M2 = 150
MICRO_MAX_AREA_M2 = 250  # üst sınır hariç; 250+ ana üretim yoludur
MICRO_MIN_PIXELS = 2
MICRO_MAX_BBOX_PIXELS = 4
MICRO_MIN_FILL_RATIO = 0.50
# Kamuya açık harita kaynaklarında Gülbahçe merkezini temsil eden yaklaşık nokta.
# Sınır/adres değildir; yalnız kapsama regresyonu ve yaklaşık izleme etiketi.
GULBAHCE_REFERENCE_POINT = (38.319473, 26.646463)
GULBAHCE_LABEL_RADIUS_M = 5_000
REPORT_REGIONS = ("cesme", "uzunkuyu")


def _contains(bbox, latitude, longitude):
    west, south, east, north = map(float, bbox)
    return west <= longitude <= east and south <= latitude <= north


def _distance_m(latitude, longitude, target):
    target_latitude, target_longitude = target
    north_m = (target_latitude - latitude) * 110570
    east_m = (
        (target_longitude - longitude)
        * 111320
        * np.cos(np.radians(latitude))
    )
    return float(np.hypot(north_m, east_m))


def _pixel_area_m2(bbox, height, width):
    west, south, east, north = map(float, bbox)
    pixel_width_m = (
        (east - west)
        * 111320
        * np.cos(np.radians((south + north) / 2))
        / width
    )
    pixel_height_m = (north - south) * 110570 / height
    return float(pixel_width_m * pixel_height_m)


def _representative_point(component, bbox, shape):
    pixels = np.asarray(component, dtype="int32")
    centroid = pixels.mean(axis=0)
    representative = pixels[int(np.argmin(np.sum((pixels - centroid) ** 2, axis=1)))]
    row, column = int(representative[0]), int(representative[1])
    height, width = shape
    west, south, east, north = map(float, bbox)
    latitude = north - (row + 0.5) / height * (north - south)
    longitude = west + (column + 0.5) / width * (east - west)
    return round(latitude, 6), round(longitude, 6)


def _compactness(component):
    pixels = np.asarray(component, dtype="int32")
    row_span = int(pixels[:, 0].max() - pixels[:, 0].min() + 1)
    col_span = int(pixels[:, 1].max() - pixels[:, 1].min() + 1)
    bbox_pixels = row_span * col_span
    fill_ratio = len(component) / max(bbox_pixels, 1)
    return bbox_pixels, float(fill_ratio)


def _strict_micro_mask(older, latest, bbox, height, width):
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
    older_scl = satellite._read_asset(
        older, "scl", bbox, height, width, "nearest"
    )[0]
    latest_scl = satellite._read_asset(
        latest, "scl", bbox, height, width, "nearest"
    )[0]

    valid = ~np.isin(older_scl, satellite.EXCLUDED_SCL_CLASSES)
    valid &= ~np.isin(latest_scl, satellite.EXCLUDED_SCL_CLASSES)
    water = (older_scl == 6) | (latest_scl == 6)
    valid &= ~satellite._dilate_mask(water, satellite.COASTAL_WATER_BUFFER_PIXELS)

    old_rgb = np.moveaxis(older_visual, 0, 2).astype("float32") / 255
    new_rgb = np.moveaxis(latest_visual, 0, 2).astype("float32") / 255
    rgb_difference = np.mean(np.abs(new_rgb - old_rgb), axis=2)
    brightness_gain = np.mean(new_rgb, axis=2) - np.mean(old_rgb, axis=2)
    older_ndvi = satellite._ndvi(older_red, older_nir)
    latest_ndvi = satellite._ndvi(latest_red, latest_nir)
    vegetation_loss = older_ndvi - latest_ndvi

    # Ana küçük-saha yolundan daha gevşek değil: aynı güçlü çoklu-spektral kapı.
    strict = (
        valid
        & (vegetation_loss > 0.20)
        & (latest_ndvi < 0.30)
        & (brightness_gain > 0.055)
        & (rgb_difference > 0.14)
    )
    return strict, rgb_difference, vegetation_loss, brightness_gain


def _micro_candidates(region_key, pair=None):
    bbox = satellite.REGIONS[region_key]["bbox"]
    older, latest = pair or satellite.sentinel_pair(region_key)
    height, width = satellite._output_shape(bbox)
    strict, rgb_difference, vegetation_loss, brightness_gain = _strict_micro_mask(
        older, latest, bbox, height, width
    )
    pixel_area = _pixel_area_m2(bbox, height, width)

    rows = []
    for component in satellite._connected_components(strict):
        if len(component) < MICRO_MIN_PIXELS:
            continue
        area_m2 = len(component) * pixel_area
        if not (MICRO_MIN_AREA_M2 <= area_m2 < MICRO_MAX_AREA_M2):
            continue
        bbox_pixels, fill_ratio = _compactness(component)
        if bbox_pixels > MICRO_MAX_BBOX_PIXELS or fill_ratio < MICRO_MIN_FILL_RATIO:
            continue

        pixels = np.asarray(component, dtype="int32")
        latitude, longitude = _representative_point(component, bbox, strict.shape)
        gulbahce_distance = _distance_m(
            latitude, longitude, GULBAHCE_REFERENCE_POINT
        )
        rows.append(
            {
                "oncelik": "MIKRO_DIAGNOSTIK",
                "alarm": False,
                "saha_gorevi": False,
                "bolge": region_key,
                "yaklasik_mevki": (
                    "Gülbahçe çevresi"
                    if gulbahce_distance <= GULBAHCE_LABEL_RADIUS_M
                    else satellite._nearest_place(latitude, longitude)
                ),
                "enlem": latitude,
                "boylam": longitude,
                "alan_m2": round(area_m2),
                "piksel": len(component),
                "bbox_piksel": bbox_pixels,
                "doluluk_orani": round(fill_ratio, 3),
                "ortalama_rgb_degisim": round(
                    float(np.mean(rgb_difference[pixels[:, 0], pixels[:, 1]])), 4
                ),
                "ortalama_ndvi_kaybi": round(
                    float(np.mean(vegetation_loss[pixels[:, 0], pixels[:, 1]])), 4
                ),
                "ortalama_parlaklik_artisi": round(
                    float(np.mean(brightness_gain[pixels[:, 0], pixels[:, 1]])), 4
                ),
                "gulbahce_merkeze_mesafe_m": round(gulbahce_distance),
                "harita": (
                    "https://www.google.com/maps/dir/?api=1&destination="
                    f"{latitude:.6f},{longitude:.6f}"
                ),
                "neden": (
                    "150-249 m² bandında, en az iki bitişik yaklaşık 10 m pikselde "
                    "ana küçük-saha yolu kadar güçlü çoklu-spektral değişim ve kompakt "
                    "geometri görüldü. Tek başına saha ziyareti nedeni değildir."
                ),
            }
        )

    rows.sort(
        key=lambda item: (
            -float(item["ortalama_rgb_degisim"]),
            -float(item["ortalama_ndvi_kaybi"]),
            float(item["alan_m2"]),
        )
    )
    return {
        "durum": "ok",
        "bolge": region_key,
        "bolge_adi": satellite.REGIONS[region_key]["label"],
        "onceki_tarih": satellite._item_date(older),
        "son_tarih": satellite._item_date(latest),
        "onceki_item": older.get("id"),
        "son_item": latest.get("id"),
        "piksel_alani_m2": round(pixel_area, 2),
        "aday_sayisi": len(rows),
        "adaylar": rows,
    }


def _self_check():
    assert satellite.MIN_HOTSPOT_AREA_M2 == 250, (
        "Ana üretim eşiği 250 m²'den değişmiş; mikro katman üretim eşiğini değiştiremez."
    )
    assert MICRO_MAX_AREA_M2 == satellite.MIN_HOTSPOT_AREA_M2
    assert MICRO_MIN_AREA_M2 == 150
    assert MICRO_MIN_PIXELS == 2
    assert _contains(
        satellite.REGIONS["uzunkuyu"]["bbox"], *GULBAHCE_REFERENCE_POINT
    ), "Gülbahçe referans merkezi günlük Uzunkuyu Sentinel kutusunun dışında kalıyor."

    horizontal = [(3, 3), (3, 4)]
    diagonal = [(3, 3), (4, 4)]
    stretched = [(3, 3), (4, 4), (5, 5)]
    assert _compactness(horizontal) == (2, 1.0)
    assert _compactness(diagonal) == (4, 0.5)
    stretched_bbox, _ = _compactness(stretched)
    assert stretched_bbox > MICRO_MAX_BBOX_PIXELS


def run_audit():
    _self_check()
    payload = {
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": satellite.MIN_HOTSPOT_AREA_M2,
        "mikro_aralik_m2": [MICRO_MIN_AREA_M2, MICRO_MAX_AREA_M2 - 1],
        "gulbahce_referans": {
            "enlem": GULBAHCE_REFERENCE_POINT[0],
            "boylam": GULBAHCE_REFERENCE_POINT[1],
            "sinir_adres_degil": True,
            "uzunkuyu_uretim_kapsaminda": _contains(
                satellite.REGIONS["uzunkuyu"]["bbox"], *GULBAHCE_REFERENCE_POINT
            ),
        },
        "not": (
            "Bu katman alarm/görev üretmez. 150-249 m² aday ancak ayrıca temporal "
            "devam/ani başlangıç veya güvenilir açık-web/yapılaşma doğrulaması alırsa "
            "09:30 saha değerlendirmesine yükseltilmelidir."
        ),
        "bolgeler": {},
    }
    for region_key in REPORT_REGIONS:
        try:
            payload["bolgeler"][region_key] = _micro_candidates(region_key)
        except Exception as exc:
            payload["bolgeler"][region_key] = {
                "durum": "hata",
                "hata": f"{type(exc).__name__}: {exc}",
                "aday_sayisi": 0,
                "adaylar": [],
            }

    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main():
    payload = run_audit()
    summary = []
    for key, data in payload["bolgeler"].items():
        summary.append(f"{key}={data.get('aday_sayisi', 0)}")
    print(
        "Mikro şantiye diagnostik taraması tamamlandı: "
        + ", ".join(summary)
        + ". Ana 250 m² alarm eşiği değişmedi; görev üretilmedi."
    )


if __name__ == "__main__":
    main()
