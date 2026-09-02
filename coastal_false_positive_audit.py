"""Saha doğrulamalı kıyı yanlış pozitiflerini Sentinel su tamponuna karşı ölçer.

Bu denetim üretim alarmını, eşiğini veya saha rotasını değiştirmez. Amaç, kullanıcı
sahasında doğrulanmış Sentinel yanlış pozitiflerinden özellikle kıyı/kayalık/ıslak
yüzey kökenli olanların mevcut yaklaşık 30 m SCL-su tamponunda neden kaldığını
ölçmek ve 40/50/60 m senaryolarının aynı Sentinel sahnesindeki küçük/erken şantiye
adaylarına olası yan etkisini görünür kılmaktır.

Tek bir saha etiketiyle küresel eşik değiştirilmez. Sonuç yalnız sonraki güvenli
kalibrasyon kararına veri sağlar.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

import rebalance_satellite_candidates as rebalance
import satellite
from daily_report import ISTANBUL
from field_outcome import ensure_outcome_schema
from field_state import ensure_state_schema
from scanner import connect


OUTPUT_FILE = Path(__file__).with_name("coastal_false_positive_audit.json")
BUFFER_RADII_PIXELS = (3, 4, 5, 6)
FALSE_POSITIVE_MATCH_METERS = 60.0
EARLY_MAX_M2 = 2_000
REGION_BY_LABEL = {
    str(data["label"]): key
    for key, data in satellite.REGIONS.items()
    if key != "all"
}


def _distance_m(lat1, lon1, lat2, lon2):
    mean_lat = math.radians((float(lat1) + float(lat2)) / 2)
    north = (float(lat1) - float(lat2)) * 110570
    east = (float(lon1) - float(lon2)) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _pixel_area_m2(bbox, height, width):
    west, south, east, north = bbox
    pixel_width_m = (
        (east - west)
        * 111320
        * math.cos(math.radians((south + north) / 2))
        / width
    )
    pixel_height_m = (north - south) * 110570 / height
    return pixel_width_m * pixel_height_m


def _date(item):
    return datetime.fromisoformat(
        item["properties"]["datetime"].replace("Z", "+00:00")
    ).strftime("%d.%m.%Y")


def _load_false_positives():
    with connect() as connection:
        ensure_outcome_schema(connection)
        ensure_state_schema(connection)
        rows = connection.execute(
            """SELECT s.gorev_id,s.enlem,s.boylam,s.alan_m2,s.bolge,
            s.onceki_tarih,s.son_tarih,s.boyut_sinifi,s.kayit_zamani,
            COALESCE(d.kaynak,'')
            FROM saha_sonuclari s
            LEFT JOIN saha_durumlari d ON d.gorev_id=s.gorev_id
            WHERE s.sonuc='YANLIS_POZITIF'
            AND s.enlem IS NOT NULL AND s.boylam IS NOT NULL
            ORDER BY s.kayit_zamani"""
        ).fetchall()

    results = []
    for row in rows:
        if str(row[9] or "") != "uydu":
            continue
        results.append(
            {
                "gorev_id": str(row[0]),
                "enlem": float(row[1]),
                "boylam": float(row[2]),
                "alan_m2": float(row[3]) if row[3] is not None else None,
                "bolge": str(row[4] or ""),
                "onceki_tarih": str(row[5] or ""),
                "son_tarih": str(row[6] or ""),
                "boyut_sinifi": str(row[7] or ""),
                "kayit_zamani": str(row[8] or ""),
            }
        )
    return results


def _prepare_region(region_key, pair):
    bbox = satellite.REGIONS[region_key]["bbox"]
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
    older_scl = satellite._read_asset(
        older, "scl", bbox, height, width, "nearest"
    )[0]
    latest_scl = satellite._read_asset(
        latest, "scl", bbox, height, width, "nearest"
    )[0]

    base_valid = ~np.isin(older_scl, satellite.EXCLUDED_SCL_CLASSES)
    base_valid &= ~np.isin(latest_scl, satellite.EXCLUDED_SCL_CLASSES)
    water = (older_scl == 6) | (latest_scl == 6)

    old_rgb = np.moveaxis(older_visual, 0, 2).astype("float32") / 255
    new_rgb = np.moveaxis(latest_visual, 0, 2).astype("float32") / 255
    rgb_difference = np.mean(np.abs(new_rgb - old_rgb), axis=2)
    brightness_gain = np.mean(new_rgb, axis=2) - np.mean(old_rgb, axis=2)
    vegetation_loss = satellite._ndvi(older_red, older_nir) - satellite._ndvi(
        latest_red, latest_nir
    )

    return {
        "bbox": bbox,
        "height": height,
        "width": width,
        "pixel_area_m2": _pixel_area_m2(bbox, height, width),
        "base_valid": base_valid,
        "water": water,
        "rgb_difference": rgb_difference,
        "brightness_gain": brightness_gain,
        "vegetation_loss": vegetation_loss,
        "latest_ndvi": satellite._ndvi(latest_red, latest_nir),
    }


def _selection_for_radius(prepared, radius):
    valid = prepared["base_valid"].copy()
    valid &= ~satellite._dilate_mask(prepared["water"], radius)

    vegetation_loss = prepared["vegetation_loss"]
    brightness_gain = prepared["brightness_gain"]
    rgb_difference = prepared["rgb_difference"]
    latest_ndvi = prepared["latest_ndvi"]

    soil_signal = (
        valid
        & (vegetation_loss > 0.14)
        & (brightness_gain > 0.035)
        & (rgb_difference > 0.10)
    )
    strong_visual_change = valid & (rgb_difference > 0.24)
    small_site_signal = (
        valid
        & (vegetation_loss > 0.20)
        & (latest_ndvi < 0.30)
        & (brightness_gain > 0.055)
        & (rgb_difference > 0.14)
    )
    change_mask = satellite._clean_mask(
        soil_signal | strong_visual_change,
        small_site_mask=small_site_signal,
    )

    raw = rebalance._uncapped_hotspots(
        change_mask,
        prepared["bbox"],
        prepared["pixel_area_m2"],
        small_site_mask=small_site_signal,
    )
    selected = rebalance._balanced_select(raw)
    return raw, selected


def _bucket_counts(items):
    values = {
        "kucuk_250_800": 0,
        "erken_800_2000": 0,
        "santiye_2000_10000": 0,
        "genis_10000_ustu": 0,
    }
    for item in items:
        try:
            area = float(item.get("alan_m2") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if area < satellite.SMALL_HOTSPOT_MAX_M2:
            values["kucuk_250_800"] += 1
        elif area <= EARLY_MAX_M2:
            values["erken_800_2000"] += 1
        elif area <= rebalance.CONSTRUCTION_SCALE_MAX_M2:
            values["santiye_2000_10000"] += 1
        else:
            values["genis_10000_ustu"] += 1
    return values


def _nearest_candidate(point, items):
    nearest = None
    nearest_distance = None
    for item in items:
        try:
            distance = _distance_m(
                point["enlem"],
                point["boylam"],
                float(item.get("enlem")),
                float(item.get("boylam")),
            )
        except (TypeError, ValueError, AttributeError):
            continue
        if nearest_distance is None or distance < nearest_distance:
            nearest = item
            nearest_distance = distance
    if nearest is None:
        return {"eslesti": False, "mesafe_m": None, "aday": None}
    return {
        "eslesti": nearest_distance <= FALSE_POSITIVE_MATCH_METERS,
        "mesafe_m": round(float(nearest_distance), 1),
        "aday": {
            "enlem": nearest.get("enlem"),
            "boylam": nearest.get("boylam"),
            "alan_m2": nearest.get("alan_m2"),
            "boyut_sinifi": nearest.get("boyut_sinifi"),
        },
    }


def _point_in_water_buffer(point, prepared, radius):
    west, south, east, north = prepared["bbox"]
    height, width = prepared["height"], prepared["width"]
    row = int((north - point["enlem"]) / (north - south) * height)
    column = int((point["boylam"] - west) / (east - west) * width)
    if not (0 <= row < height and 0 <= column < width):
        return None
    expanded = satellite._dilate_mask(prepared["water"], radius)
    return bool(expanded[row, column])


def _self_check():
    sample = {
        "enlem": 38.3000,
        "boylam": 26.3000,
    }
    candidates = [
        {"enlem": 38.3001, "boylam": 26.3000, "alan_m2": 300},
        {"enlem": 38.3100, "boylam": 26.3100, "alan_m2": 1200},
    ]
    nearest = _nearest_candidate(sample, candidates)
    assert nearest["eslesti"], nearest
    counts = _bucket_counts(
        [
            {"alan_m2": 300},
            {"alan_m2": 1000},
            {"alan_m2": 5000},
            {"alan_m2": 12000},
        ]
    )
    assert counts == {
        "kucuk_250_800": 1,
        "erken_800_2000": 1,
        "santiye_2000_10000": 1,
        "genis_10000_ustu": 1,
    }, counts


def build_audit():
    _self_check()
    false_positives = _load_false_positives()
    grouped = {}
    for item in false_positives:
        region_key = REGION_BY_LABEL.get(item.get("bolge"))
        if region_key:
            grouped.setdefault(region_key, []).append(item)

    regions = {}
    analyzed_total = 0
    for region_key, points in grouped.items():
        pair = satellite.sentinel_pair(region_key)
        current_dates = (_date(pair[0]), _date(pair[1]))
        same_scene = [
            point for point in points
            if (point.get("onceki_tarih"), point.get("son_tarih")) == current_dates
        ]
        skipped_scene = [
            point for point in points
            if point not in same_scene
        ]
        if not same_scene:
            regions[region_key] = {
                "bolge": satellite.REGIONS[region_key]["label"],
                "mevcut_cift": list(current_dates),
                "ayni_sahne_yanlis_pozitif": 0,
                "farkli_sahne_etiketi": len(skipped_scene),
                "senaryolar": {},
                "yanlis_pozitifler": [],
            }
            continue

        prepared = _prepare_region(region_key, pair)
        scenarios = {}
        selections = {}
        for radius in BUFFER_RADII_PIXELS:
            raw, selected = _selection_for_radius(prepared, radius)
            selections[radius] = (raw, selected)
            scenarios[str(radius)] = {
                "tampon_m_yaklasik": radius * satellite.TARGET_PIXEL_SIZE_M,
                "ham_aday": len(raw),
                "secili_aday": len(selected),
                "ham_olcek": _bucket_counts(raw),
                "secili_olcek": _bucket_counts(selected),
            }

        point_reports = []
        for point in same_scene:
            radius_results = {}
            for radius in BUFFER_RADII_PIXELS:
                raw, selected = selections[radius]
                radius_results[str(radius)] = {
                    "su_tamponunda": _point_in_water_buffer(point, prepared, radius),
                    "ham_en_yakin": _nearest_candidate(point, raw),
                    "secili_en_yakin": _nearest_candidate(point, selected),
                }
            point_reports.append({**point, "yaricap_sonuclari": radius_results})
            analyzed_total += 1

        regions[region_key] = {
            "bolge": satellite.REGIONS[region_key]["label"],
            "mevcut_cift": list(current_dates),
            "analiz_piksel_m_yaklasik": round(
                math.sqrt(prepared["pixel_area_m2"]), 2
            ),
            "ayni_sahne_yanlis_pozitif": len(same_scene),
            "farkli_sahne_etiketi": len(skipped_scene),
            "senaryolar": scenarios,
            "yanlis_pozitifler": point_reports,
        }

    return {
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "amac": (
            "Saha doğrulamalı Sentinel yanlış pozitiflerini mevcut 30 m ve yalnız "
            "diagnostik 40/50/60 m SCL-su tamponlarında karşılaştırmak; üretimi değiştirmez."
        ),
        "uretim_degistirildi": False,
        "mevcut_tampon_piksel": satellite.COASTAL_WATER_BUFFER_PIXELS,
        "denenen_tampon_piksel": list(BUFFER_RADII_PIXELS),
        "sentinel_yanlis_pozitif_toplam": len(false_positives),
        "mevcut_sahneyle_analiz_edilen": analyzed_total,
        "karar_kurali": (
            "Tek saha etiketiyle küresel tampon büyütülmez. Ölçüm, aynı mekanizma "
            "birden fazla saha sonucunda tekrar ederse ve erken/parsel aday kaybı "
            "göstermiyorsa manuel üretim değişikliği incelemesine veri sağlar."
        ),
        "bolgeler": regions,
    }


def main():
    report = build_audit()
    OUTPUT_FILE.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
