"""Kuru/seyrek bitkili zeminde mevcut Sentinel filtresinin kaçırabileceği küçük değişimi ölçer.

Çeşme yaz sonunda çok kuru olduğu için yeni kazı her zaman belirgin bir NDVI kaybı
üretmeyebilir. Ana motor 250 m²+ alarm eşiğini, küçük-saha filtresini veya görev
listesini burada değiştirmiyoruz. Yalnız mevcut değişim maskesinin dışında kalan,
hem önce hem sonra düşük NDVI taşıyan ve Bare Soil Index (BSI) ile gerçek renk
farkında eşzamanlı spektral değişim gösteren 250-2.000 m² kümeleri sayıyoruz.

Ham kuru-zemin sinyali yaz sonunda çok sayıda tarla/toprak değişimi üretebildiği için
bu diagnostik katman ayrıca küme geometrisini ölçer. Kompakt/dolgun ve aşırı lineer
olmayan bileşenler "saha benzeri geometri" olarak işaretlenir; yakın çevredeki diğer
kuru-zemin bileşenlerinin sayısı da kaydedilir. Ana Sentinel motorunun su/kıyı tamponuna
düşen değişimler silinmez: arka planda ölçülür, ancak kıyı-karışık piksel riski nedeniyle
"izole saha benzeri" kanıtına yükseltilmez. Bu etiketler yalnız kalibrasyon ve saha geri
bildirimi içindir; hiçbir kayıt bu nedenle alarma/göreve dönüştürülmez veya ana radar
önceliği değiştirilmez.
"""

from __future__ import annotations

import argparse
import json
import math
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

# Bu eşikler üretim filtresi değildir. Ham kuru-zemin körlük havuzunu, saha sonucu
# geldiğinde hangi şekil sinyalinin işe yaradığını ölçebilmek için alt gruplara ayırır.
SITE_LIKE_MAX_ASPECT = 3.5
SITE_LIKE_MIN_FILL = 0.35
SITE_LIKE_MIN_COMPACTNESS = 0.18
LINEAR_RISK_MIN_ASPECT = 4.0
LINEAR_RISK_MAX_COMPACTNESS = 0.12
LOCAL_CLUSTER_RADIUS_M = 120


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


def _perimeter_edges(component):
    pixels = set(component)
    exposed = 0
    for row, column in component:
        exposed += (row - 1, column) not in pixels
        exposed += (row + 1, column) not in pixels
        exposed += (row, column - 1) not in pixels
        exposed += (row, column + 1) not in pixels
    return max(int(exposed), 1)


def _shape_metrics(component):
    pixels = np.asarray(component, dtype="int32")
    rows = pixels[:, 0]
    columns = pixels[:, 1]
    row_span = int(rows.max() - rows.min() + 1)
    col_span = int(columns.max() - columns.min() + 1)
    short_span = max(min(row_span, col_span), 1)
    long_span = max(row_span, col_span)
    aspect_ratio = float(long_span / short_span)
    fill_ratio = float(len(component) / max(row_span * col_span, 1))
    perimeter = _perimeter_edges(component)
    compactness = float(4 * math.pi * len(component) / (perimeter * perimeter))
    site_like = (
        aspect_ratio <= SITE_LIKE_MAX_ASPECT
        and fill_ratio >= SITE_LIKE_MIN_FILL
        and compactness >= SITE_LIKE_MIN_COMPACTNESS
    )
    linear_risk = (
        aspect_ratio >= LINEAR_RISK_MIN_ASPECT
        or compactness <= LINEAR_RISK_MAX_COMPACTNESS
    )
    return {
        "uzun_kisa_orani": round(aspect_ratio, 2),
        "kutu_doluluk_orani": round(fill_ratio, 3),
        "kompaktlik": round(compactness, 3),
        "saha_benzeri_geometri": bool(site_like),
        "lineer_geometri_riski": bool(linear_risk),
    }


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _component_overlap_fraction(component, mask):
    """Bileşenin verilen risk maskesiyle kesişen piksel oranını döndürür."""
    if mask is None or not component:
        return 0.0
    pixels = np.asarray(component, dtype="int32")
    return float(np.mean(mask[pixels[:, 0], pixels[:, 1]]))


def _records(
    mask,
    bbox,
    pixel_area_m2,
    bsi_delta,
    rgb_difference,
    coastal_buffer=None,
):
    records = []
    for component in satellite._connected_components(mask):
        area_m2 = len(component) * pixel_area_m2
        if not (satellite.MIN_HOTSPOT_AREA_M2 <= area_m2 <= MAX_AUDIT_AREA_M2):
            continue
        pixels = np.asarray(component, dtype="int32")
        latitude, longitude = _component_location(component, bbox, mask.shape)
        coastal_fraction = _component_overlap_fraction(component, coastal_buffer)
        record = {
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
            "kiyi_su_tamponu_riski": bool(coastal_fraction > 0),
            "kiyi_su_tamponu_orani": round(coastal_fraction, 3),
        }
        record.update(_shape_metrics(component))
        record["yakindaki_kuru_degisim_120m"] = 0
        records.append(record)

    # Çok sayıda birbirine yakın küçük kuru-zemin parçası tarla sürümü, geniş alan
    # hazırlığı veya görüntü/geometri etkisi olabilir. Bunu yalnız bağlamsal sinyal
    # olarak ölçüyoruz; yakın kümeler gerçek bir toplu şantiye de olabilir.
    for index, first in enumerate(records):
        first_point = (float(first["enlem"]), float(first["boylam"]))
        neighbors = 0
        for second_index, second in enumerate(records):
            if index == second_index:
                continue
            second_point = (float(second["enlem"]), float(second["boylam"]))
            if _distance_m(first_point, second_point) <= LOCAL_CLUSTER_RADIUS_M:
                neighbors += 1
        first["yakindaki_kuru_degisim_120m"] = neighbors
        # Kıyı/su tamponuna değen kayıt diagnostik havuzda kalır; ancak ana üretim
        # motorunun bilinçli olarak dışladığı karma kıyı pikseli, 15 Eylül sonrası
        # kuru-zemin teyit yolunda "izole saha" kanıtına dönüşemez.
        first["izole_saha_benzeri"] = bool(
            first.get("saha_benzeri_geometri")
            and neighbors == 0
            and not first.get("kiyi_su_tamponu_riski")
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
    water = (older_scl == 6) | (latest_scl == 6)
    coastal_buffer = satellite._dilate_mask(
        water,
        satellite.COASTAL_WATER_BUFFER_PIXELS,
    )
    # Gerçek üretim maskesi kıyı tamponunu dışlar. Diagnostik havuz ise tamponu
    # tamamen silmez; aşağıda risk etiketiyle arka planda tutar.
    production_valid = valid & ~coastal_buffer

    old_rgb = np.moveaxis(older_visual, 0, 2).astype("float32") / 255
    new_rgb = np.moveaxis(latest_visual, 0, 2).astype("float32") / 255
    rgb_difference = np.mean(np.abs(new_rgb - old_rgb), axis=2)
    brightness_gain = np.mean(new_rgb, axis=2) - np.mean(old_rgb, axis=2)
    older_ndvi = satellite._ndvi(older_red, older_nir)
    latest_ndvi = satellite._ndvi(latest_red, latest_nir)

    soil_signal = (
        production_valid
        & (older_ndvi - latest_ndvi > 0.14)
        & (brightness_gain > 0.035)
        & (rgb_difference > 0.10)
    )
    strong_visual_change = production_valid & (rgb_difference > 0.24)
    small_site_signal = (
        production_valid
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
        coastal_buffer=coastal_buffer,
    )
    site_like = [item for item in records if item.get("saha_benzeri_geometri")]
    isolated_site_like = [item for item in site_like if item.get("izole_saha_benzeri")]
    linear_risk = [item for item in records if item.get("lineer_geometri_riski")]
    coastal_risk = [item for item in records if item.get("kiyi_su_tamponu_riski")]
    site_like_examples = sorted(
        site_like,
        key=lambda item: (
            bool(item.get("kiyi_su_tamponu_riski")),
            int(item.get("yakindaki_kuru_degisim_120m") or 0),
            -float(item.get("ortalama_bsi_degisim") or 0),
            -float(item.get("ortalama_rgb_farki") or 0),
        ),
    )[:EXAMPLE_LIMIT]

    return {
        "bolge": region["label"],
        "onceki_item": older.get("id"),
        "son_item": latest.get("id"),
        "onceki_tarih": satellite._item_date(older),
        "son_tarih": satellite._item_date(latest),
        "durum": "ok",
        "potansiyel_kuru_zemin_korlugu": len(records),
        "saha_benzeri_geometri": len(site_like),
        "izole_saha_benzeri_geometri": len(isolated_site_like),
        "lineer_geometri_riski": len(linear_risk),
        "kiyi_su_tamponu_riski": len(coastal_risk),
        "ornekler": records[:EXAMPLE_LIMIT],
        "saha_benzeri_ornekler": site_like_examples,
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

    square = [(r, c) for r in range(3) for c in range(3)]
    strip = [(0, c) for c in range(9)]
    square_shape = _shape_metrics(square)
    strip_shape = _shape_metrics(strip)
    assert square_shape["saha_benzeri_geometri"], square_shape
    assert not square_shape["lineer_geometri_riski"], square_shape
    assert strip_shape["lineer_geometri_riski"], strip_shape
    assert not strip_shape["saha_benzeri_geometri"], strip_shape
    assert _distance_m((38.30, 26.30), (38.30, 26.30)) == 0

    coastal = np.zeros((3, 3), dtype=bool)
    coastal[1, 1] = True
    assert _component_overlap_fraction([(1, 1), (1, 2)], coastal) == 0.5
    assert _component_overlap_fraction([(0, 0)], coastal) == 0.0


def run_audit():
    _self_check()
    report_date = datetime.now(ISTANBUL).strftime("%Y-%m-%d")
    payload = {
        "rapor_tarihi": report_date,
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "amac": (
            "Mevcut alarm üretim maskesinin dışında kalan düşük-NDVI 250-2.000 m² "
            "kuru zemin spektral değişimlerini BSI ile ölçmek; ayrıca kompaktlık, "
            "uzun/kısa oranı, 120 m yerel kümelenme ve ana Sentinel kıyı tamponu "
            "riskini saha kalibrasyonu için kaydetmek; alarm/görev üretmez."
        ),
        "esikler": {
            "alan_m2": [satellite.MIN_HOTSPOT_AREA_M2, MAX_AUDIT_AREA_M2],
            "dusuk_ndvi_max": LOW_NDVI_MAX,
            "max_ndvi_degisim": MAX_NDVI_CHANGE,
            "rgb_farki": [MIN_RGB_DIFFERENCE, MAX_RGB_DIFFERENCE],
            "min_mutlak_parlaklik_degisim": MIN_ABS_BRIGHTNESS_CHANGE,
            "min_mutlak_bsi_degisim": MIN_ABS_BSI_CHANGE,
            "saha_benzeri_max_uzun_kisa": SITE_LIKE_MAX_ASPECT,
            "saha_benzeri_min_kutu_doluluk": SITE_LIKE_MIN_FILL,
            "saha_benzeri_min_kompaktlik": SITE_LIKE_MIN_COMPACTNESS,
            "lineer_risk_min_uzun_kisa": LINEAR_RISK_MIN_ASPECT,
            "lineer_risk_max_kompaktlik": LINEAR_RISK_MAX_COMPACTNESS,
            "yerel_kume_yaricapi_m": LOCAL_CLUSTER_RADIUS_M,
            "kiyi_su_tamponu_piksel": satellite.COASTAL_WATER_BUFFER_PIXELS,
            "kiyi_su_tamponu_m_yaklasik": (
                satellite.COASTAL_WATER_BUFFER_PIXELS * satellite.TARGET_PIXEL_SIZE_M
            ),
        },
        "uyari": (
            "Geometri, yakınlık ve kıyı etiketleri yalnız diagnostiktir. Kıyı/su tamponuna "
            "değen kayıt silinmez ve arka planda izlenir; fakat izole-saha kanıtına "
            "yükseltilmez. Saha doğrulaması olmadan hiçbir kuru-zemin kaydı alarm/görev "
            "değildir ve ana üretim radarı etkilenmez."
        ),
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
        print(
            "Kuru zemin körlük denetimi öz testi başarılı; üretim alarmı değişmedi, "
            "saha-benzeri geometri, yerel kümelenme ve kıyı tamponu riski diagnostik "
            "olarak ölçülüyor."
        )
        return

    payload = run_audit()
    counts = []
    for region_key, data in payload.get("bolgeler", {}).items():
        if data.get("durum") == "ok":
            counts.append(
                f"{region_key}={int(data.get('potansiyel_kuru_zemin_korlugu') or 0)} "
                f"(saha-benzeri={int(data.get('saha_benzeri_geometri') or 0)}, "
                f"izole={int(data.get('izole_saha_benzeri_geometri') or 0)}, "
                f"lineer-risk={int(data.get('lineer_geometri_riski') or 0)}, "
                f"kıyı-risk={int(data.get('kiyi_su_tamponu_riski') or 0)})"
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
