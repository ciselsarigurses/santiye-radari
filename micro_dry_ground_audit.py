"""150-249 m² kuru-zemin mikro körlüğünü alarm üretmeden ölçer.

Ana MİKRO ŞANTİYE maskesi özellikle güçlü NDVI kaybı + parlaklık artışı arar.
Yaz sonunda zaten kuru/seyrek bitkili bir parselde hafriyat başladığında NDVI fazla
oynamadan BSI (Bare Soil Index) ve gerçek renk değişebilir. 250 m²+ için bu boşluğu
``dry_ground_gap_audit.py`` ölçüyor; bu dosya aynı fikrin yalnız 150-249 m² bandındaki
iki-piksellik, alarm-dışı karşılığını ölçer.

Ham iki-piksel sinyal tek başına aday değildir. Güçlü diagnostik sayılabilmesi için:
- 150-249 m² ve kompakt/saha-benzeri geometri,
- kıyı/su tamponundan uzaklık,
- 9x9 bağlam halkasına göre lokal BSI kontrastı,
- bir önceki Sentinel dönemine göre ani başlangıç desteği,
- üç sahnede yeterli geçerli piksel ve istikrarsız eski zemin riski olmaması
birlikte gerekir.

Çıktı alarm/saha görevi üretmez, 250 m² ana eşiği değiştirmez ve mevcut mikro karar
zincirine otomatik terfi vermez. Amaç, 15 Eylül sonrası gerçek kazı başlarken kuru
zemin nedeniyle kaçabilecek mikro sinyal sınıfını ölçülebilir hale getirmektir.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np

import satellite
import dry_ground_gap_audit as dry
import dry_ground_temporal_audit as temporal
from daily_report import ISTANBUL


OUTPUT_FILE = Path(__file__).with_name("micro_dry_ground_audit.json")
REPORT_REGIONS = ("cesme", "uzunkuyu")
MICRO_MIN_AREA_M2 = 150
MICRO_MAX_AREA_M2 = 250  # üst sınır hariç
MICRO_MIN_PIXELS = 2
CONTEXT_RADIUS_PIXELS = 4
CENTER_RADIUS_PIXELS = 1
MIN_CONTEXT_VALID_FRACTION = 0.55
LOCAL_BSI_TARGET_MIN = 0.12
LOCAL_BSI_CONTRAST_MIN = 2.5
BROAD_CONTEXT_BSI_Q75_MIN = 0.08
BROAD_CONTEXT_ACTIVE_BSI_MIN = 0.10
BROAD_CONTEXT_ACTIVE_FRACTION_MIN = 0.25
BROAD_CONTEXT_CONTRAST_MAX = 1.7
GULBAHCE_POINT = (38.33278, 26.64556)
GULBAHCE_RADIUS_M = 2_000


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


def _scene(item, bbox, height, width):
    visual = satellite._read_asset(item, "visual", bbox, height, width, "bilinear")[:3]
    blue = satellite._reflectance(
        satellite._read_asset(item, "blue", bbox, height, width, "bilinear")[0]
    )
    red = satellite._reflectance(
        satellite._read_asset(item, "red", bbox, height, width, "bilinear")[0]
    )
    nir = satellite._reflectance(
        satellite._read_asset(item, "nir", bbox, height, width, "bilinear")[0]
    )
    swir = satellite._reflectance(
        satellite._read_asset(item, "swir16", bbox, height, width, "bilinear")[0]
    )
    scl = satellite._read_asset(item, "scl", bbox, height, width, "nearest")[0]
    rgb = np.moveaxis(visual, 0, 2).astype("float32") / 255
    ndvi = satellite._ndvi(red, nir)
    brightness = np.mean(rgb, axis=2)
    bsi = dry._bsi(blue, red, nir, swir)
    return rgb, ndvi, brightness, bsi, scl


def _window_slices(row, column, shape, radius):
    height, width = shape
    return (
        slice(max(0, row - radius), min(height, row + radius + 1)),
        slice(max(0, column - radius), min(width, column + radius + 1)),
    )


def _local_bsi_metrics(bsi_delta, valid, component):
    pixels = np.asarray(component, dtype="int32")
    centroid = pixels.mean(axis=0)
    representative = pixels[int(np.argmin(np.sum((pixels - centroid) ** 2, axis=1)))]
    row, column = map(int, representative)
    row_slice, col_slice = _window_slices(
        row, column, valid.shape, CONTEXT_RADIUS_PIXELS
    )
    local_valid = valid[row_slice, col_slice]
    local_delta = bsi_delta[row_slice, col_slice]
    height, width = local_valid.shape
    center_row = row - (row_slice.start or 0)
    center_col = column - (col_slice.start or 0)
    rr, cc = np.ogrid[:height, :width]
    center_mask = (
        (np.abs(rr - center_row) <= CENTER_RADIUS_PIXELS)
        & (np.abs(cc - center_col) <= CENTER_RADIUS_PIXELS)
    )
    context_valid = local_valid & ~center_mask
    context_values = local_delta[context_valid]
    if not context_values.size:
        return None

    target_values = bsi_delta[pixels[:, 0], pixels[:, 1]]
    target = float(np.mean(target_values))
    q75 = float(np.quantile(context_values, 0.75))
    mean = float(np.mean(context_values))
    active_fraction = float(np.mean(context_values >= BROAD_CONTEXT_ACTIVE_BSI_MIN))
    valid_fraction = float(
        context_values.size / max(int((~center_mask).sum()), 1)
    )
    contrast = target / max(q75, 0.03)
    broad_risk = bool(
        valid_fraction >= MIN_CONTEXT_VALID_FRACTION
        and q75 >= BROAD_CONTEXT_BSI_Q75_MIN
        and active_fraction >= BROAD_CONTEXT_ACTIVE_FRACTION_MIN
        and contrast <= BROAD_CONTEXT_CONTRAST_MAX
    )
    compact_support = bool(
        valid_fraction >= MIN_CONTEXT_VALID_FRACTION
        and target >= LOCAL_BSI_TARGET_MIN
        and contrast >= LOCAL_BSI_CONTRAST_MIN
        and not broad_risk
    )
    return {
        "baglam_gecerli_oran": round(valid_fraction, 3),
        "hedef_bsi_degisim": round(target, 4),
        "cevre_q75_bsi_degisim": round(q75, 4),
        "cevre_ortalama_bsi_degisim": round(mean, 4),
        "cevre_aktif_bsi_orani": round(active_fraction, 3),
        "yerel_bsi_kontrast_orani": round(contrast, 2),
        "genis_yuzey_kontekst_riski": broad_risk,
        "lokal_bsi_destegi": compact_support,
    }


def _component_records(raw_mask, bbox, pixel_area, arrays, coastal_buffer):
    bsi_delta, rgb_difference = arrays
    records = []
    for component in satellite._connected_components(raw_mask):
        if len(component) < MICRO_MIN_PIXELS:
            continue
        area_m2 = len(component) * pixel_area
        if not (MICRO_MIN_AREA_M2 <= area_m2 < MICRO_MAX_AREA_M2):
            continue
        latitude, longitude = dry._component_location(component, bbox, raw_mask.shape)
        pixels = np.asarray(component, dtype="int32")
        coastal_fraction = dry._component_overlap_fraction(component, coastal_buffer)
        item = {
            "alarm": False,
            "saha_gorevi": False,
            "mahalle": satellite._nearest_place(latitude, longitude),
            "enlem": latitude,
            "boylam": longitude,
            "alan_m2": round(area_m2),
            "piksel": len(component),
            "ortalama_bsi_degisim": round(
                float(np.mean(bsi_delta[pixels[:, 0], pixels[:, 1]])), 4
            ),
            "ortalama_rgb_farki": round(
                float(np.mean(rgb_difference[pixels[:, 0], pixels[:, 1]])), 4
            ),
            "kiyi_su_tamponu_riski": bool(coastal_fraction > 0),
            "kiyi_su_tamponu_orani": round(coastal_fraction, 3),
            "gulbahce_2km": dry._distance_m(
                (latitude, longitude), GULBAHCE_POINT
            ) <= GULBAHCE_RADIUS_M,
            "_component": component,
        }
        item.update(dry._shape_metrics(component))
        records.append(item)

    for index, first in enumerate(records):
        first_point = (float(first["enlem"]), float(first["boylam"]))
        neighbors = 0
        for second_index, second in enumerate(records):
            if index == second_index:
                continue
            second_point = (float(second["enlem"]), float(second["boylam"]))
            if dry._distance_m(first_point, second_point) <= dry.LOCAL_CLUSTER_RADIUS_M:
                neighbors += 1
        first["yakindaki_mikro_kuru_degisim_120m"] = neighbors
    return records


def _analyze_region(region_key):
    bbox = satellite.REGIONS[region_key]["bbox"]
    older, latest = satellite.sentinel_pair(region_key)
    items = satellite._search_items(bbox)
    older_match = temporal._find_item(items, older.get("id")) or older
    latest_match = temporal._find_item(items, latest.get("id")) or latest
    previous = temporal._previous_scene(items, older_match, bbox)
    if previous is None:
        return {
            "durum": "atlandi",
            "neden": "degisim_oncesi_uygun_sentinel_sahnesi_bulunamadi",
            "onceki_item": older.get("id"),
            "son_item": latest.get("id"),
        }

    height, width = satellite._output_shape(bbox)
    previous_rgb, previous_ndvi, previous_brightness, previous_bsi, previous_scl = _scene(
        previous, bbox, height, width
    )
    older_rgb, older_ndvi, older_brightness, older_bsi, older_scl = _scene(
        older_match, bbox, height, width
    )
    latest_rgb, latest_ndvi, latest_brightness, latest_bsi, latest_scl = _scene(
        latest_match, bbox, height, width
    )

    quality_valid = ~np.isin(older_scl, satellite.EXCLUDED_SCL_CLASSES)
    quality_valid &= ~np.isin(latest_scl, satellite.EXCLUDED_SCL_CLASSES)
    water = (older_scl == 6) | (latest_scl == 6)
    coastal_buffer = satellite._dilate_mask(water, satellite.COASTAL_WATER_BUFFER_PIXELS)
    production_valid = quality_valid & ~coastal_buffer

    rgb_difference = np.mean(np.abs(latest_rgb - older_rgb), axis=2)
    brightness_gain = latest_brightness - older_brightness
    vegetation_loss = older_ndvi - latest_ndvi
    soil_signal = (
        production_valid
        & (vegetation_loss > 0.14)
        & (brightness_gain > 0.035)
        & (rgb_difference > 0.10)
    )
    strong_visual_change = production_valid & (rgb_difference > 0.24)
    small_site_signal = (
        production_valid
        & (vegetation_loss > 0.20)
        & (latest_ndvi < 0.30)
        & (brightness_gain > 0.055)
        & (rgb_difference > 0.14)
    )
    production_mask = satellite._clean_mask(
        soil_signal | strong_visual_change,
        small_site_mask=small_site_signal,
    )

    current_bsi_delta = np.abs(latest_bsi - older_bsi)
    previous_bsi_delta = np.abs(older_bsi - previous_bsi)
    ndvi_delta = np.abs(latest_ndvi - older_ndvi)
    diagnostic_raw = (
        quality_valid
        & ~production_mask
        & (older_ndvi < dry.LOW_NDVI_MAX)
        & (latest_ndvi < dry.LOW_NDVI_MAX)
        & (ndvi_delta < dry.MAX_NDVI_CHANGE)
        & (rgb_difference > dry.MIN_RGB_DIFFERENCE)
        & (rgb_difference < dry.MAX_RGB_DIFFERENCE)
        & (np.abs(brightness_gain) > dry.MIN_ABS_BRIGHTNESS_CHANGE)
        & (current_bsi_delta > dry.MIN_ABS_BSI_CHANGE)
    )
    micro_mask = satellite._retain_components(diagnostic_raw, MICRO_MIN_PIXELS)
    pixel_area = _pixel_area_m2(bbox, height, width)
    records = _component_records(
        micro_mask,
        bbox,
        pixel_area,
        (current_bsi_delta, rgb_difference),
        coastal_buffer,
    )

    temporal_valid = ~np.isin(previous_scl, satellite.EXCLUDED_SCL_CLASSES)
    temporal_valid &= ~np.isin(older_scl, satellite.EXCLUDED_SCL_CLASSES)
    temporal_valid &= ~np.isin(latest_scl, satellite.EXCLUDED_SCL_CLASSES)
    temporal_water = (previous_scl == 6) | (older_scl == 6) | (latest_scl == 6)
    temporal_valid &= ~satellite._dilate_mask(
        temporal_water, satellite.COASTAL_WATER_BUFFER_PIXELS
    )

    analyzed = []
    for raw in records:
        component = raw.pop("_component")
        row, column = temporal._pixel_for_point(
            raw["enlem"], raw["boylam"], bbox, current_bsi_delta.shape
        )
        row_slice, col_slice = temporal._patch_slices(
            row, column, current_bsi_delta.shape
        )
        previous_mean, current_mean, valid_fraction = temporal._paired_patch_means(
            previous_bsi_delta,
            current_bsi_delta,
            temporal_valid,
            row_slice,
            col_slice,
        )
        ratio, abrupt, unstable = temporal._classify(
            current_mean, previous_mean, valid_fraction
        )
        locality = _local_bsi_metrics(
            current_bsi_delta,
            production_valid,
            component,
        ) or {
            "baglam_gecerli_oran": 0.0,
            "hedef_bsi_degisim": None,
            "cevre_q75_bsi_degisim": None,
            "cevre_ortalama_bsi_degisim": None,
            "cevre_aktif_bsi_orani": None,
            "yerel_bsi_kontrast_orani": None,
            "genis_yuzey_kontekst_riski": False,
            "lokal_bsi_destegi": False,
        }
        item = dict(raw)
        item.update(locality)
        item.update(
            {
                "onceki_donem_bsi_degisim": (
                    round(previous_mean, 4) if previous_mean is not None else None
                ),
                "son_donem_bsi_degisim": (
                    round(current_mean, 4) if current_mean is not None else None
                ),
                "uc_sahne_gecerli_oran": round(valid_fraction, 3),
                "ani_baslangic_orani": ratio,
                "ani_baslangic_destegi": abrupt,
                "istikrarsiz_zemin_riski": unstable,
            }
        )
        strong = bool(
            item.get("saha_benzeri_geometri")
            and not item.get("lineer_geometri_riski")
            and not item.get("kiyi_su_tamponu_riski")
            and item.get("lokal_bsi_destegi")
            and not item.get("genis_yuzey_kontekst_riski")
            and item.get("ani_baslangic_destegi")
            and not item.get("istikrarsiz_zemin_riski")
        )
        item["karar_sinifi"] = (
            "MIKRO_KURU_ZEMIN_GUCLU_DIAGNOSTIK" if strong
            else "MIKRO_KURU_ZEMIN_ARKA_PLAN"
        )
        item["mikro_guclu_diagnostik"] = strong
        item["alarm"] = False
        item["saha_gorevi"] = False
        analyzed.append(item)

    analyzed.sort(
        key=lambda item: (
            not bool(item.get("mikro_guclu_diagnostik")),
            bool(item.get("genis_yuzey_kontekst_riski")),
            bool(item.get("kiyi_su_tamponu_riski")),
            -float(item.get("yerel_bsi_kontrast_orani") or 0),
            -float(item.get("ani_baslangic_orani") or 0),
            -float(item.get("ortalama_bsi_degisim") or 0),
        )
    )
    return {
        "durum": "ok",
        "bolge": satellite.REGIONS[region_key]["label"],
        "onceki_onceki_item": previous.get("id"),
        "onceki_onceki_tarih": satellite._item_date(previous),
        "onceki_item": older_match.get("id"),
        "onceki_tarih": satellite._item_date(older_match),
        "son_item": latest_match.get("id"),
        "son_tarih": satellite._item_date(latest_match),
        "ham_mikro_kuru_zemin": len(analyzed),
        "guclu_mikro_kuru_zemin_diagnostik": sum(
            bool(item.get("mikro_guclu_diagnostik")) for item in analyzed
        ),
        "gulbahce_2km_ham": sum(bool(item.get("gulbahce_2km")) for item in analyzed),
        "gulbahce_2km_guclu": sum(
            bool(item.get("gulbahce_2km")) and bool(item.get("mikro_guclu_diagnostik"))
            for item in analyzed
        ),
        "adaylar": analyzed,
    }


def _self_check():
    assert satellite.MIN_HOTSPOT_AREA_M2 == 250
    assert MICRO_MIN_AREA_M2 == 150 and MICRO_MAX_AREA_M2 == 250
    assert MICRO_MIN_PIXELS == 2
    bsi = np.zeros((9, 9), dtype="float32")
    valid = np.ones((9, 9), dtype=bool)
    component = [(4, 4), (4, 5)]
    bsi[4, 4] = 0.20
    bsi[4, 5] = 0.18
    metrics = _local_bsi_metrics(bsi, valid, component)
    assert metrics and metrics["lokal_bsi_destegi"]
    broad = np.full((9, 9), 0.11, dtype="float32")
    broad[4, 4] = 0.14
    broad[4, 5] = 0.14
    broad_metrics = _local_bsi_metrics(broad, valid, component)
    assert broad_metrics and broad_metrics["genis_yuzey_kontekst_riski"]
    ratio, abrupt, unstable = temporal._classify(0.16, 0.04, 1.0)
    assert ratio >= 2 and abrupt and not unstable
    ratio, abrupt, unstable = temporal._classify(0.16, 0.13, 1.0)
    assert not abrupt and unstable


def run_audit():
    _self_check()
    payload = {
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": satellite.MIN_HOTSPOT_AREA_M2,
        "mikro_aralik_m2": [MICRO_MIN_AREA_M2, MICRO_MAX_AREA_M2 - 1],
        "amac": (
            "Ana mikro NDVI-kaybı maskesinin dışında kalabilecek, zaten kuru zeminde "
            "başlayan 150-249 m² değişimleri BSI + lokal bağlam + üç-sahne ani başlangıç "
            "kanıtıyla yalnız diagnostik olarak ölçmek."
        ),
        "esikler": {
            "minimum_piksel": MICRO_MIN_PIXELS,
            "dusuk_ndvi_max": dry.LOW_NDVI_MAX,
            "max_ndvi_degisim": dry.MAX_NDVI_CHANGE,
            "min_bsi_degisim": dry.MIN_ABS_BSI_CHANGE,
            "lokal_bsi_hedef_min": LOCAL_BSI_TARGET_MIN,
            "lokal_bsi_kontrast_min": LOCAL_BSI_CONTRAST_MIN,
            "genis_cevre_q75_min": BROAD_CONTEXT_BSI_Q75_MIN,
            "genis_cevre_aktif_oran_min": BROAD_CONTEXT_ACTIVE_FRACTION_MIN,
            "ani_baslangic_oran_min": temporal.ABRUPT_RATIO_MIN,
            "onceki_bsi_mutlak_tavan": temporal.PRECHANGE_ABS_BSI_CAP,
            "kiyi_su_tamponu_piksel": satellite.COASTAL_WATER_BUFFER_PIXELS,
        },
        "uyari": (
            "MIKRO_KURU_ZEMIN_GUCLU_DIAGNOSTIK bile alarm veya saha görevi değildir. "
            "Tarla/toprak temizliği ve geniş homojen hareketler bağlam riski olarak arka "
            "planda tutulur; statü/adres/parsel çıkarımı yapılmaz."
        ),
        "bolgeler": {},
    }
    for region_key in REPORT_REGIONS:
        try:
            payload["bolgeler"][region_key] = _analyze_region(region_key)
        except Exception as exc:  # diagnostik katman ana taramayı düşürmesin
            payload["bolgeler"][region_key] = {
                "durum": "hata",
                "neden": f"{type(exc).__name__}: {exc}",
            }
    OUTPUT_FILE.write_text(
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
        print("Mikro kuru-zemin diagnostik öz testi başarılı.")
        return
    payload = run_audit()
    for region_key, data in payload["bolgeler"].items():
        if data.get("durum") != "ok":
            print(f"{region_key}: {data.get('durum')} · {data.get('neden')}")
            continue
        print(
            f"{region_key}: ham mikro kuru-zemin={data['ham_mikro_kuru_zemin']}, "
            f"güçlü diagnostik={data['guclu_mikro_kuru_zemin_diagnostik']}, "
            f"Gülbahçe 2km={data['gulbahce_2km_ham']}/"
            f"{data['gulbahce_2km_guclu']} güçlü"
        )


if __name__ == "__main__":
    main()
