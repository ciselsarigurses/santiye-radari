"""Ana Sentinel saha adaylarında koordinat temsil hassasiyetini ölçer.

Üretim alarmını, 250 m² eşiğini, aday sıralamasını veya saha görevlerini değiştirmez.
Güncel ``latest_report.json`` içindeki yeni Sentinel adaylarını, üretimde kullanılan
aynı değişim maskesinde tekrar bulur. Mevcut geometrik temsil pikseli ile aynı bağlı
bileşendeki üretim-sinyali marjlarına göre hesaplanan ağırlıklı temsil pikseli arasındaki
mesafeyi raporlar.

Amaç, doğru koordinat hedefini körlemesine değiştirerek değil; önce hangi gerçek saha
adaylarında 10 m Sentinel piksel ölçeğinde anlamlı bir koordinat iyileştirme payı olduğunu
ölçerek ilerletmektir. Önerilen nokta her zaman aynı bağlı bileşendeki gerçek bir Sentinel
piksel merkezidir ve tek başına kesin adres/parsel değildir.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

import satellite


REPORT_PATH = Path(__file__).with_name("latest_report.json")
OUTPUT_PATH = Path(__file__).with_name("hotspot_coordinate_audit.json")
AREA_SIMILARITY_MIN = 0.80
MAX_CURRENT_GEOMETRIC_M = 5.0


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _area_similarity(first, second):
    first = max(float(first), 0.0)
    second = max(float(second), 0.0)
    maximum = max(first, second)
    if maximum <= 0:
        return 1.0
    return min(first, second) / maximum


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
    """Sinyal-ağırlıklı merkeze en yakın gerçek bileşen pikselini seç."""
    pixels = np.asarray(component, dtype="int32")
    values = score[pixels[:, 0], pixels[:, 1]].astype("float64")
    values = np.where(np.isfinite(values) & (values > 0), values, 0.0)
    if float(values.sum()) <= 0:
        return _representative_pixel(component)
    weighted = np.average(pixels.astype("float64"), axis=0, weights=values)
    distance = np.sum((pixels - weighted) ** 2, axis=1)
    representative = pixels[int(np.argmin(distance))]
    return int(representative[0]), int(representative[1])


def _production_geometry(region_key, pair):
    """``satellite.analyze_sentinel_change`` ile aynı üretim maskesini yeniden kur."""
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

    # Mutlak bir yeni eşik icat etmek yerine üretimde zaten kullanılan koşulların
    # ne kadar aşıldığını ağırlık olarak kullan. Böylece ağırlıklı nokta, maskeyi
    # oluşturan kanıtın bileşen içindeki merkezini temsil eder.
    score = (
        np.maximum(vegetation_loss - 0.14, 0)
        + np.maximum(brightness_gain - 0.035, 0)
        + np.maximum(rgb_difference - 0.10, 0)
        + np.maximum(rgb_difference - 0.24, 0)
    )
    score = np.where(change_mask, score, 0.0)

    west, south, east, north = bbox
    pixel_width_m = (
        (east - west)
        * 111320
        * np.cos(np.radians((south + north) / 2))
        / width
    )
    pixel_height_m = (north - south) * 110570 / height
    pixel_area_m2 = float(pixel_width_m * pixel_height_m)

    components = satellite._connected_components(change_mask)
    lookup = {}
    for index, component in enumerate(components):
        for row, column in component:
            lookup[(int(row), int(column))] = index

    return bbox, change_mask.shape, components, lookup, score, pixel_area_m2


def _analyze_region(region_key, report):
    label = satellite.REGIONS[region_key]["label"]
    pair = satellite.sentinel_pair(region_key)
    latest_date = satellite._item_date(pair[1])
    bbox, shape, components, lookup, score, pixel_area_m2 = _production_geometry(
        region_key, pair
    )

    rows = []
    unmatched = []
    seen = set()
    for raw in report.get("saha_adaylari") or []:
        if not isinstance(raw, dict):
            continue
        if raw.get("bolge") != label or not raw.get("yeni_goruntu"):
            continue
        if str(raw.get("son_tarih") or "") != latest_date:
            continue
        try:
            area = float(raw.get("alan_m2") or 0)
            latitude = float(raw.get("enlem"))
            longitude = float(raw.get("boylam"))
        except (TypeError, ValueError):
            continue
        if area < satellite.MIN_HOTSPOT_AREA_M2:
            continue

        key = (round(latitude, 6), round(longitude, 6))
        if key in seen:
            continue
        seen.add(key)
        pixel = _point_pixel(latitude, longitude, bbox, shape)
        component_id = lookup.get(pixel)
        if component_id is None:
            unmatched.append(
                {
                    "gorev_id": raw.get("gorev_id"),
                    "mahalle": raw.get("mahalle"),
                    "enlem": round(latitude, 6),
                    "boylam": round(longitude, 6),
                    "alan_m2": round(area),
                    "neden": "rapor_koordinati_guncel_uretim_maskesinde_bilesen_icine_dusmuyor",
                }
            )
            continue

        component = components[component_id]
        component_area = len(component) * pixel_area_m2
        geometric_pixel = _representative_pixel(component)
        geometric_point = _pixel_location(*geometric_pixel, bbox, shape)
        current_point = (round(latitude, 6), round(longitude, 6))
        area_similarity = _area_similarity(area, component_area)
        current_geometric_m = _distance_m(current_point, geometric_point)

        # Yan-küme, dedupe veya başka bir post-process adayı daha büyük ham üretim
        # bileşeninin içine taşıyabilir. Böyle bir kaydı ham bileşenin merkezine göre
        # ölçmek sahte 100+ m "koordinat hatası" üretir. Yalnız alanı ve mevcut
        # geometrik temsil noktası ham üretim bileşeniyle gerçekten uyuşan adaylar
        # koordinat hassasiyet istatistiğine girsin; diğerleri ayrı diagnostik olarak
        # görünür kalsın.
        if area_similarity < AREA_SIMILARITY_MIN or current_geometric_m > MAX_CURRENT_GEOMETRIC_M:
            unmatched.append(
                {
                    "gorev_id": raw.get("gorev_id"),
                    "mahalle": raw.get("mahalle"),
                    "enlem": current_point[0],
                    "boylam": current_point[1],
                    "rapor_alan_m2": round(area),
                    "bilesen_alan_m2": round(component_area),
                    "alan_benzerligi": round(area_similarity, 3),
                    "mevcut_geometrik_fark_m": round(current_geometric_m, 1),
                    "neden": "aday_ham_uretim_bileseniyle_birebir_eslesmiyor_postprocess_veya_yan_kume_olabilir",
                }
            )
            continue

        weighted_pixel = _weighted_representative_pixel(component, score)
        weighted_point = _pixel_location(*weighted_pixel, bbox, shape)
        rows.append(
            {
                "gorev_id": raw.get("gorev_id"),
                "oncelik": raw.get("oncelik"),
                "mahalle": raw.get("mahalle"),
                "rapor_alan_m2": round(area),
                "bilesen_alan_m2": round(component_area),
                "alan_benzerligi": round(area_similarity, 3),
                "bilesen_piksel": len(component),
                "mevcut_enlem": current_point[0],
                "mevcut_boylam": current_point[1],
                "geometrik_enlem": geometric_point[0],
                "geometrik_boylam": geometric_point[1],
                "sinyal_agirlikli_enlem": weighted_point[0],
                "sinyal_agirlikli_boylam": weighted_point[1],
                "mevcut_geometrik_fark_m": round(current_geometric_m, 1),
                "geometrik_sinyal_kaymasi_m": round(
                    _distance_m(geometric_point, weighted_point), 1
                ),
            }
        )

    rows.sort(
        key=lambda item: -float(item.get("geometrik_sinyal_kaymasi_m") or 0)
    )
    return {
        "bolge": label,
        "onceki_item": pair[0].get("id"),
        "son_item": pair[1].get("id"),
        "son_tarih": latest_date,
        "olculen_aday": len(rows),
        "eslesmeyen_aday": len(unmatched),
        "10m_ustu_kayma": sum(
            1 for item in rows
            if float(item.get("geometrik_sinyal_kaymasi_m") or 0) > 10
        ),
        "20m_ustu_kayma": sum(
            1 for item in rows
            if float(item.get("geometrik_sinyal_kaymasi_m") or 0) > 20
        ),
        "maksimum_kayma_m": max(
            [float(item.get("geometrik_sinyal_kaymasi_m") or 0) for item in rows]
            or [0]
        ),
        "adaylar": rows,
        "eslesmeyenler": unmatched,
    }


def build_audit():
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    regions = {}
    errors = {}
    for region_key in ("cesme", "uzunkuyu"):
        try:
            regions[region_key] = _analyze_region(region_key, report)
        except Exception as exc:  # Diagnostik bir bölge yüzünden diğerini kaybetmesin.
            errors[region_key] = f"{type(exc).__name__}: {exc}"

    measured = sum(int(item.get("olculen_aday") or 0) for item in regions.values())
    over_10 = sum(int(item.get("10m_ustu_kayma") or 0) for item in regions.values())
    over_20 = sum(int(item.get("20m_ustu_kayma") or 0) for item in regions.values())
    unmatched = sum(int(item.get("eslesmeyen_aday") or 0) for item in regions.values())
    return {
        "rapor_tarihi": report.get("rapor_tarihi"),
        "amac": "Ana 250 m²+ Sentinel saha adaylarında mevcut geometrik koordinat ile aynı üretim bileşenindeki sinyal-ağırlıklı gerçek piksel koordinatı arasındaki farkı ölçmek.",
        "politika": "DIAGNOSTIK_ONLY: alarm, görev, sıralama ve 250 m² ana eşik değişmez; saha kanıtı olmadan koordinat otomatik taşınmaz.",
        "ham_bilesenle_birebir_olculen_aday": measured,
        "postprocess_veya_eslesmeyen_aday": unmatched,
        "10m_ustu_kayma": over_10,
        "20m_ustu_kayma": over_20,
        "bolgeler": regions,
        "hatalar": errors,
    }


def _self_test():
    component = [(1, 1), (1, 2), (1, 3)]
    score = np.zeros((4, 5), dtype="float32")
    score[1, 3] = 10
    assert _representative_pixel(component) == (1, 2)
    assert _weighted_representative_pixel(component, score) == (1, 3)
    fallback = np.zeros((4, 5), dtype="float32")
    assert _weighted_representative_pixel(component, fallback) == (1, 2)
    assert _area_similarity(400, 400) == 1.0
    assert _area_similarity(3100, 38500) < AREA_SIMILARITY_MIN
    print("Ana aday koordinat hassasiyet öz testi başarılı.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        return

    audit = build_audit()
    OUTPUT_PATH.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Ana aday koordinat denetimi: "
        f"{audit['ham_bilesenle_birebir_olculen_aday']} birebir aday, "
        f"post-process/eşleşmeyen {audit['postprocess_veya_eslesmeyen_aday']}, "
        f">10 m {audit['10m_ustu_kayma']}, >20 m {audit['20m_ustu_kayma']}."
    )
    if audit.get("hatalar"):
        print("Bölgesel diagnostik hataları:", audit["hatalar"])


if __name__ == "__main__":
    main()
