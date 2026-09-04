"""Güçlü 150-249 m² mikro adaylarda koordinat çekirdeğini yalnız diagnostik ölçer.

Ana Sentinel saha alarm eşiği 250 m² olarak kalır. Mikro adayın mevcut koordinatı
bağlı bileşeni temsil eden piksel merkezidir; iki-dört piksellik küçük bir bileşende
spektral değişimin en kuvvetli olduğu piksel komşu piksel olabilir. Bu denetim,
mevcut koordinatı değiştirmeden aynı bağlı bileşendeki en güçlü çoklu-spektral
pikseli ikinci bir ``sinyal çekirdeği`` koordinatı olarak ölçer.

Sonuç alarm veya saha görevi üretmez. Amaç, yeni Sentinel sahnesinde tekrar doğrulama
yapılırken 10 m sınıfı piksel belirsizliğini açıkça görmek ve gereksiz koordinat
kaymasını erken yakalamaktır.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

import micro_site_audit as micro
import satellite


REVIEW_FILE = Path(__file__).with_name("micro_site_footprint_priority_review.json")
OUTPUT_FILE = Path(__file__).with_name("micro_site_coordinate_audit.json")
ISTANBUL = ZoneInfo("Europe/Istanbul")
MAIN_THRESHOLD_M2 = 250
MICRO_MIN_AREA_M2 = 150
MICRO_MAX_AREA_M2 = 250
MATCH_MAX_METERS = 15.0
MATCH_AREA_TOLERANCE_M2 = 80.0
CORE_SHIFT_REVIEW_METERS = 15.0

# _strict_micro_mask içindeki değişmeyen spektral kapılarla aynı normalizasyon.
RGB_GATE = 0.14
VEGETATION_LOSS_GATE = 0.20
BRIGHTNESS_GAIN_GATE = 0.055


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return float(math.hypot(north, east))


def _pixel_center(row, column, bbox, shape):
    height, width = shape
    west, south, east, north = map(float, bbox)
    latitude = north - (row + 0.5) / height * (north - south)
    longitude = west + (column + 0.5) / width * (east - west)
    return round(latitude, 6), round(longitude, 6)


def _core_pixel(component, rgb_difference, vegetation_loss, brightness_gain):
    pixels = np.asarray(component, dtype="int32")
    rows = pixels[:, 0]
    columns = pixels[:, 1]
    score = (
        rgb_difference[rows, columns] / RGB_GATE
        + vegetation_loss[rows, columns] / VEGETATION_LOSS_GATE
        + brightness_gain[rows, columns] / BRIGHTNESS_GAIN_GATE
    )
    index = int(np.argmax(score))
    row = int(rows[index])
    column = int(columns[index])
    return row, column, float(score[index])


def _component_records(region_key):
    bbox = satellite.REGIONS[region_key]["bbox"]
    older, latest = satellite.sentinel_pair(region_key)
    height, width = satellite._output_shape(bbox)
    strict, rgb_difference, vegetation_loss, brightness_gain = micro._strict_micro_mask(
        older,
        latest,
        bbox,
        height,
        width,
    )
    pixel_area = micro._pixel_area_m2(bbox, height, width)

    records = []
    for component in satellite._connected_components(strict):
        if len(component) < micro.MICRO_MIN_PIXELS:
            continue
        area_m2 = len(component) * pixel_area
        if not (MICRO_MIN_AREA_M2 <= area_m2 < MICRO_MAX_AREA_M2):
            continue
        bbox_pixels, fill_ratio = micro._compactness(component)
        if (
            bbox_pixels > micro.MICRO_MAX_BBOX_PIXELS
            or fill_ratio < micro.MICRO_MIN_FILL_RATIO
        ):
            continue

        representative = micro._representative_point(component, bbox, strict.shape)
        core_row, core_column, core_score = _core_pixel(
            component,
            rgb_difference,
            vegetation_loss,
            brightness_gain,
        )
        core = _pixel_center(core_row, core_column, bbox, strict.shape)
        pixels = np.asarray(component, dtype="int32")
        records.append(
            {
                "temsilci": representative,
                "cekirdek": core,
                "alan_m2": round(area_m2),
                "piksel": len(component),
                "bbox_piksel": bbox_pixels,
                "doluluk_orani": round(fill_ratio, 3),
                "cekirdek_skor": round(core_score, 4),
                "cekirdek_rgb_degisim": round(
                    float(rgb_difference[core_row, core_column]), 4
                ),
                "cekirdek_ndvi_kaybi": round(
                    float(vegetation_loss[core_row, core_column]), 4
                ),
                "cekirdek_parlaklik_artisi": round(
                    float(brightness_gain[core_row, core_column]), 4
                ),
                "ortalama_rgb_degisim": round(
                    float(np.mean(rgb_difference[pixels[:, 0], pixels[:, 1]])), 4
                ),
            }
        )

    return {
        "older_item": str(older.get("id") or ""),
        "latest_item": str(latest.get("id") or ""),
        "records": records,
    }


def _match(candidate, records):
    point = (float(candidate["enlem"]), float(candidate["boylam"]))
    area = float(candidate.get("alan_m2") or 0.0)
    ranked = []
    for record in records:
        distance = _distance_m(point, record["temsilci"])
        area_gap = abs(area - float(record["alan_m2"]))
        if distance <= MATCH_MAX_METERS and area_gap <= MATCH_AREA_TOLERANCE_M2:
            ranked.append((distance, area_gap, record))
    if not ranked:
        return None
    ranked.sort(key=lambda row: (row[0], row[1]))
    return ranked[0][2]


def build_audit(payload):
    if int(payload.get("ana_uretim_esigi_m2") or 0) != MAIN_THRESHOLD_M2:
        raise RuntimeError("Mikro koordinat denetiminde 250 m² ana eşik invariantı bozuldu.")
    interval = list(payload.get("mikro_aralik_m2") or [])
    if interval != [150, 249]:
        raise RuntimeError("Mikro koordinat denetiminde 150-249 m² bant invariantı bozuldu.")

    strong = [
        item
        for item in (payload.get("guclu_adaylar") or [])
        if isinstance(item, dict)
        and bool(item.get("mikro_footprint_guclu_diagnostik"))
        and MICRO_MIN_AREA_M2 <= float(item.get("alan_m2") or 0) < MICRO_MAX_AREA_M2
    ]

    region_cache = {}
    rows = []
    unmatched = []
    for candidate in strong:
        region_key = str(candidate.get("bolge") or "")
        if region_key not in satellite.REGIONS:
            unmatched.append({"neden": "BOLGE_BILINMIYOR", "aday": candidate})
            continue
        if region_key not in region_cache:
            region_cache[region_key] = _component_records(region_key)
        matched = _match(candidate, region_cache[region_key]["records"])
        if matched is None:
            unmatched.append(
                {
                    "neden": "GUNCEL_BAGLI_BILESEN_ESLESMEDI",
                    "bolge": region_key,
                    "enlem": candidate.get("enlem"),
                    "boylam": candidate.get("boylam"),
                    "alan_m2": candidate.get("alan_m2"),
                }
            )
            continue

        representative = (
            float(candidate["enlem"]),
            float(candidate["boylam"]),
        )
        core = matched["cekirdek"]
        shift = _distance_m(representative, core)
        if shift <= 1.0:
            quality = "AYNI_PIKSEL"
        elif shift <= CORE_SHIFT_REVIEW_METERS:
            quality = "KOMSULUK_ICI_10M"
        else:
            quality = "KOORDINAT_INCELE"

        rows.append(
            {
                "alarm": False,
                "saha_gorevi": False,
                "bolge": region_key,
                "yaklasik_mevki": candidate.get("yaklasik_mevki"),
                "alan_m2": candidate.get("alan_m2"),
                "enlem": round(representative[0], 6),
                "boylam": round(representative[1], 6),
                "sinyal_cekirdegi_enlem": round(float(core[0]), 6),
                "sinyal_cekirdegi_boylam": round(float(core[1]), 6),
                "sinyal_cekirdegi_sapma_m": round(shift, 1),
                "koordinat_kalitesi": quality,
                "cekirdek_skor": matched["cekirdek_skor"],
                "cekirdek_rgb_degisim": matched["cekirdek_rgb_degisim"],
                "cekirdek_ndvi_kaybi": matched["cekirdek_ndvi_kaybi"],
                "cekirdek_parlaklik_artisi": matched[
                    "cekirdek_parlaklik_artisi"
                ],
                "temsilci_harita": (
                    "https://www.google.com/maps/search/?api=1&query="
                    f"{representative[0]:.6f},{representative[1]:.6f}"
                ),
                "cekirdek_harita": (
                    "https://www.google.com/maps/search/?api=1&query="
                    f"{float(core[0]):.6f},{float(core[1]):.6f}"
                ),
                "latest_item": region_cache[region_key]["latest_item"],
            }
        )

    rows.sort(
        key=lambda item: (
            0 if item["koordinat_kalitesi"] == "KOORDINAT_INCELE" else 1,
            -float(item["sinyal_cekirdegi_sapma_m"]),
        )
    )
    review_count = sum(item["koordinat_kalitesi"] == "KOORDINAT_INCELE" for item in rows)
    max_shift = max((float(item["sinyal_cekirdegi_sapma_m"]) for item in rows), default=0.0)

    return {
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": [150, 249],
        "amac": (
            "Güçlü mikro diagnostik adayın bağlı bileşen temsilcisi ile aynı "
            "bileşendeki en güçlü çoklu-spektral piksel merkezini karşılaştırmak."
        ),
        "uyari": (
            "Sinyal çekirdeği ikinci diagnostik koordinattır; alarm/görev üretmez, "
            "adres veya parsel değildir ve mevcut temsilci koordinatını otomatik değiştirmez."
        ),
        "guclu_aday": len(strong),
        "eslesen": len(rows),
        "eslesmeyen": len(unmatched),
        "koordinat_incele": review_count,
        "maksimum_sapma_m": round(max_shift, 1),
        "adaylar": rows,
        "eslesmeyen_adaylar": unmatched,
    }


def _without_runtime(payload):
    clean = dict(payload)
    clean.pop("olusturma", None)
    return clean


def _write_if_changed(result):
    if OUTPUT_FILE.exists():
        try:
            current = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            current = {}
        if _without_runtime(current) == _without_runtime(result):
            print("Mikro koordinat çekirdeği değişmedi; çıktı yeniden yazılmadı.")
            return False
    OUTPUT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def _self_check():
    assert satellite.MIN_HOTSPOT_AREA_M2 == MAIN_THRESHOLD_M2
    component = [(0, 0), (0, 1)]
    rgb = np.array([[0.20, 0.40]], dtype="float32")
    vegetation = np.array([[0.25, 0.30]], dtype="float32")
    brightness = np.array([[0.08, 0.12]], dtype="float32")
    row, column, score = _core_pixel(component, rgb, vegetation, brightness)
    assert (row, column) == (0, 1)
    assert score > 0
    assert _distance_m((38.0, 26.0), (38.0, 26.0)) == 0.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Mikro koordinat çekirdeği öz testi başarılı; 250 m² eşik/alarm/görev değişmedi.")
        return

    if not REVIEW_FILE.exists():
        raise RuntimeError(f"{REVIEW_FILE.name} bulunamadı.")
    payload = json.loads(REVIEW_FILE.read_text(encoding="utf-8"))
    result = build_audit(payload)
    _write_if_changed(result)
    print(
        "Mikro koordinat çekirdeği denetimi: "
        f"güçlü={result['guclu_aday']}, eşleşen={result['eslesen']}, "
        f"incele={result['koordinat_incele']}, maksimum_sapma={result['maksimum_sapma_m']} m. "
        "Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
