"""Gülbahçe'de gerçekten en yeni Sentinel sahnesinde görünmeyen alanları doğrular.

MİKRO taramasındaki kalite maskesi iki sahnenin birlikte geçerli olmasını ister.
Bu, temporal değişim hesabı için doğrudur; ancak "en yeni sahne kör" demek için
yeterli değildir. Bu koruma yalnız en yeni SCL sahnesini inceler ve kıyı/su
yanlışlarını azaltmak için son açık Sentinel tarihlerindeki SCL=4/5 kara kanıtını
bağımsız referans olarak kullanır.

Alarm veya saha görevi üretmez. 250 m² ana üretim eşiğini ve 150-249 m² MİKRO
politikasını değiştirmez.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

import satellite
from coverage_blind_area_audit import _historical_surface_mask, _pixel_area_m2
from micro_site_audit import (
    GULBAHCE_OPERATION_POINT,
    GULBAHCE_OPERATION_RADIUS_M,
    _circle_mask,
    _representative_point,
)


MICRO_BLIND_JSON = Path(__file__).with_name("gulbahce_micro_blind_capacity.json")
COVERAGE_JSON = Path(__file__).with_name("coverage_blind_area_audit.json")
REPORT_JSON = Path(__file__).with_name("latest_report.json")
OUTPUT_JSON = Path(__file__).with_name("gulbahce_latest_state_blind_review.json")

MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
PATROL_MAX_AREA_M2 = 6500
MATCH_RADIUS_M = 35

CLOUD_CLASSES = np.array([8, 9, 10], dtype="uint8")
SHADOW_CLASSES = np.array([2, 3], dtype="uint8")
NO_DATA_CLASSES = np.array([0, 1], dtype="uint8")
SNOW_CLASS = 11
WATER_CLASS = 6


def _point(item):
    try:
        return float(item.get("enlem")), float(item.get("boylam"))
    except (AttributeError, TypeError, ValueError):
        return None


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _coverage_east(payload):
    regions = (payload or {}).get("bolgeler") or {}
    direct = regions.get("uzunkuyu")
    if isinstance(direct, dict):
        return direct
    for value in regions.values():
        if not isinstance(value, dict):
            continue
        label = str(value.get("bolge") or "")
        if "gülbahçe" in label.casefold():
            return value
    return {}


def _component_reason(component, latest_scl):
    pixels = np.asarray(component, dtype="int32")
    values = latest_scl[pixels[:, 0], pixels[:, 1]]
    if float(np.mean(np.isin(values, NO_DATA_CLASSES))) >= 0.50:
        return "NO_DATA_VEYA_DOYGUN"
    if float(np.mean(np.isin(values, SHADOW_CLASSES))) >= 0.50:
        return "GOLGE"
    if float(np.mean(np.isin(values, CLOUD_CLASSES))) >= 0.50:
        return "BULUT"
    if float(np.mean(values == SNOW_CLASS)) >= 0.50:
        return "KAR_VEYA_BUZ"
    return "KARISIK_GECERSIZ_SCL"


def _component_rows(mask, bbox, pixel_area, latest_scl, surface_class):
    rows = []
    for component in satellite._connected_components(mask):
        area_m2 = len(component) * pixel_area
        if area_m2 < MICRO_RANGE_M2[0]:
            continue
        latitude, longitude = _representative_point(component, bbox, mask.shape)
        rows.append(
            {
                "enlem": latitude,
                "boylam": longitude,
                "alan_m2": int(round(area_m2)),
                "neden": _component_reason(component, latest_scl),
                "yuzey_kaniti": surface_class,
                "alarm": False,
                "saha_gorevi": False,
            }
        )
    rows.sort(key=lambda item: (item["alan_m2"], item["enlem"], item["boylam"]))
    return rows


def _split_area(rows):
    micro = [
        row for row in rows
        if MICRO_RANGE_M2[0] <= int(row["alan_m2"]) <= MICRO_RANGE_M2[1]
    ]
    main = [
        row for row in rows
        if MAIN_THRESHOLD_M2 <= int(row["alan_m2"]) <= PATROL_MAX_AREA_M2
    ]
    large = [
        row for row in rows
        if int(row["alan_m2"]) > PATROL_MAX_AREA_M2
    ]
    return micro, main, large


def _attach_patrol_distance(rows, report_payload):
    guard = (report_payload or {}).get("gulbahce_kor_alan_devriye_korumasi") or {}
    selected = guard.get("secilen") if isinstance(guard, dict) else None
    selected_point = _point(selected) if isinstance(selected, dict) else None

    result = []
    for item in rows:
        point = _point(item)
        distance = _distance_m(point, selected_point) if point and selected_point else None
        row = dict(item)
        row["secilen_devriyeye_mesafe_m"] = round(distance) if distance is not None else None
        row["secilen_devriye_kapsiyor"] = bool(
            distance is not None and distance <= MATCH_RADIUS_M
        )
        result.append(row)
    return result


def build_review_from_masks(
    *,
    bbox,
    latest_scl,
    known_land,
    known_water,
    unknown_surface,
    operation,
    pixel_area,
    report_payload,
    source_date,
    source_item,
    coverage_date,
    coverage_item,
    reference_dates,
    pair_blind_count=0,
):
    if latest_scl.shape != known_land.shape:
        raise ValueError("SCL ve tarihsel yüzey maskesi boyutları uyuşmuyor.")

    latest_quality_invalid = np.isin(latest_scl, satellite.EXCLUDED_SCL_CLASSES)
    latest_water = latest_scl == WATER_CLASS

    # Gerçek güncel-sahne kalite körlüğü yalnız tarihsel olarak kara olduğu
    # bağımsız sahnelerde kanıtlanmış piksellerde hesaplanır.
    land_blind_mask = operation & known_land & latest_quality_invalid
    # Tarihsel kara iken en yeni SCL'nin su demesi kalite körlüğü değildir;
    # kıyı/su/ıslak yüzey arka-plan sınıfında ayrı tutulur.
    historical_land_current_water_mask = operation & known_land & latest_water
    water_invalid_mask = operation & known_water & latest_quality_invalid
    unknown_invalid_mask = operation & unknown_surface & latest_quality_invalid

    land_rows = _component_rows(
        land_blind_mask, bbox, pixel_area, latest_scl, "TARIHSEL_KARA"
    )
    water_rows = _component_rows(
        water_invalid_mask, bbox, pixel_area, latest_scl, "TARIHSEL_SU"
    )
    unknown_rows = _component_rows(
        unknown_invalid_mask, bbox, pixel_area, latest_scl, "COZULMEMIS_YUZEY"
    )
    land_current_water_rows = _component_rows(
        historical_land_current_water_mask,
        bbox,
        pixel_area,
        latest_scl,
        "TARIHSEL_KARA_GUNCEL_SU",
    )
    for row in land_current_water_rows:
        row["neden"] = "GUNCEL_SCL_SU"

    micro_rows, main_rows, large_rows = _split_area(land_rows)
    main_rows = _attach_patrol_distance(main_rows, report_payload)
    matched_count = sum(1 for item in main_rows if item["secilen_devriye_kapsiyor"])

    same_scene = bool(
        source_date
        and source_item
        and source_date == coverage_date
        and source_item == coverage_item
    )

    return {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_santiye_araligi_m2": MICRO_RANGE_M2,
        "durum": "ok" if same_scene else "veri_tarihi_uyusmuyor",
        "amac": (
            "Gülbahçe 2 km operasyon alanında yalnız EN YENİ Sentinel SCL sahnesinde "
            "kalite nedeniyle görülemeyen pikselleri, son açık tarihlerde SCL=4/5 ile "
            "bağımsız olarak kara olduğu kanıtlanmış yüzeylerde ölçmek; iki-sahne "
            "temporal körlüğünü güncel-sahne körlüğüyle karıştırmamak."
        ),
        "kaynak_son_tarih": source_date or None,
        "kaynak_son_item": source_item or None,
        "coverage_son_tarih": coverage_date or None,
        "coverage_son_item": coverage_item or None,
        "ayni_sentinel_sahnesi": same_scene,
        "kara_referans_sahne_sayisi": len(reference_dates or []),
        "kara_referans_tarihleri": list(reference_dates or []),
        "ikili_sahne_korlugu_250plus_diagnostik": int(pair_blind_count or 0),
        "guncel_sahne_mikro_kor_150_249": len(micro_rows),
        "guncel_sahne_kor_250_6500": len(main_rows),
        "guncel_sahne_kor_6500ustu": len(large_rows),
        "secilen_devriye_eslesen_kor_kume": matched_count,
        "secilen_devriye_guncel_korlugu_kapsiyor": matched_count > 0,
        "guncel_sahne_mikro_kor_kumeleri": micro_rows,
        "guncel_sahne_kor_kumeleri": main_rows,
        "guncel_sahne_buyuk_kor_kumeleri": large_rows[:8],
        "tarihsel_su_kalite_kor_150plus": len(water_rows),
        "cozulmemis_yuzey_kalite_kor_150plus": len(unknown_rows),
        "tarihsel_kara_guncel_su_150plus": len(land_current_water_rows),
        "tarihsel_su_ornekleri": water_rows[:8],
        "cozulmemis_yuzey_ornekleri": unknown_rows[:8],
        "tarihsel_kara_guncel_su_ornekleri": land_current_water_rows[:8],
        "yorum": (
            "Bu sonuç alarm/görev değildir. MİKRO temporal analizde iki sahnenin de "
            "geçerli olması gerekir; bu yüzden ikili-sahne körlüğü ayrı bir diagnostiktir. "
            "Buradaki 'güncel-sahne kör' sayısı yalnız en yeni sahnenin geçersiz olduğu "
            "ve tarihsel kara kanıtı bulunan alanları içerir. Tarihsel su, çözülememiş "
            "yüzey ve tarihsel kara olup güncel SCL=su görünen alanlar şantiye körlüğüne "
            "karıştırılmaz; arka planda ayrı tutulur."
        ),
    }


def analyze_current_scene(micro_payload, coverage_payload, report_payload):
    bbox = satellite.REGIONS["uzunkuyu"]["bbox"]
    _older, latest = satellite.sentinel_pair("uzunkuyu")
    height, width = satellite._output_shape(bbox)
    latest_scl = satellite._read_asset(
        latest, "scl", bbox, height, width, "nearest"
    )[0]
    pixel_area = _pixel_area_m2(bbox, height, width)
    operation = _circle_mask(
        bbox,
        latest_scl.shape,
        GULBAHCE_OPERATION_POINT,
        GULBAHCE_OPERATION_RADIUS_M,
    )

    items = satellite._search_items(bbox)
    known_land, known_water, unknown_surface, refs, ref_dates = (
        _historical_surface_mask(items, latest, bbox, height, width)
    )
    if not refs:
        raise RuntimeError("Gülbahçe güncel-sahne kara/su referansı üretilemedi.")

    coverage_east = _coverage_east(coverage_payload)
    source_date = satellite._item_date(latest)
    source_item = str(latest.get("id") or "")
    coverage_date = str(coverage_east.get("son_tarih") or "")
    coverage_item = str(coverage_east.get("son_item") or "")

    pair_blind_count = int(
        (micro_payload or {}).get("ana_kor_kume_250plus") or 0
    )

    return build_review_from_masks(
        bbox=bbox,
        latest_scl=latest_scl,
        known_land=known_land,
        known_water=known_water,
        unknown_surface=unknown_surface,
        operation=operation,
        pixel_area=pixel_area,
        report_payload=report_payload,
        source_date=source_date,
        source_item=source_item,
        coverage_date=coverage_date,
        coverage_item=coverage_item,
        reference_dates=ref_dates,
        pair_blind_count=pair_blind_count,
    )


def _self_check():
    assert satellite.MIN_HOTSPOT_AREA_M2 == MAIN_THRESHOLD_M2
    bbox = (26.64, 38.32, 26.65, 38.33)
    shape = (6, 6)
    latest_scl = np.full(shape, 4, dtype="uint8")
    known_land = np.zeros(shape, dtype=bool)
    known_water = np.zeros(shape, dtype=bool)
    unknown = np.zeros(shape, dtype=bool)
    operation = np.ones(shape, dtype=bool)

    # 2 piksellik tarihsel kara + bulut kümesi: 200 m² MİKRO körlük.
    known_land[1, 1:3] = True
    latest_scl[1, 1:3] = 9

    # 3 piksellik tarihsel kara + gölge kümesi: 300 m² ana körlük.
    known_land[3, 1:4] = True
    latest_scl[3, 1:4] = 3

    # Tarihsel su üzerindeki bulut ana körlük sayılmamalı.
    known_water[5, 0:3] = True
    latest_scl[5, 0:3] = 9

    # Çözülememiş yüzey ayrı tutulmalı.
    unknown[0, 3:5] = True
    latest_scl[0, 3:5] = 10

    report = {
        "gulbahce_kor_alan_devriye_korumasi": {
            "secilen": {"enlem": 38.324167, "boylam": 26.6425}
        }
    }
    result = build_review_from_masks(
        bbox=bbox,
        latest_scl=latest_scl,
        known_land=known_land,
        known_water=known_water,
        unknown_surface=unknown,
        operation=operation,
        pixel_area=100.0,
        report_payload=report,
        source_date="05.09.2026",
        source_item="S2_TEST",
        coverage_date="05.09.2026",
        coverage_item="S2_TEST",
        reference_dates=["05.09.2026", "03.09.2026"],
        pair_blind_count=4,
    )
    assert result["durum"] == "ok"
    assert result["guncel_sahne_mikro_kor_150_249"] == 1
    assert result["guncel_sahne_kor_250_6500"] == 1
    assert result["tarihsel_su_kalite_kor_150plus"] == 1
    assert result["cozulmemis_yuzey_kalite_kor_150plus"] == 1
    assert result["ikili_sahne_korlugu_250plus_diagnostik"] == 4
    assert all(
        item["yuzey_kaniti"] == "TARIHSEL_KARA"
        for item in (
            result["guncel_sahne_mikro_kor_kumeleri"]
            + result["guncel_sahne_kor_kumeleri"]
        )
    )
    assert result["alarm"] is False and result["saha_gorevi"] is False

    stale = build_review_from_masks(
        bbox=bbox,
        latest_scl=latest_scl,
        known_land=known_land,
        known_water=known_water,
        unknown_surface=unknown,
        operation=operation,
        pixel_area=100.0,
        report_payload={},
        source_date="05.09.2026",
        source_item="S2_TEST",
        coverage_date="03.09.2026",
        coverage_item="OLD",
        reference_dates=["05.09.2026"],
    )
    assert stale["durum"] == "veri_tarihi_uyusmuyor"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if args.check_only:
        print(
            "Gülbahçe gerçek güncel-sahne körlük öz testi başarılı; "
            "tarihsel kara/su ayrımı korundu, 250 m² eşik değişmedi."
        )
        return

    for path in (MICRO_BLIND_JSON, COVERAGE_JSON, REPORT_JSON):
        if not path.exists():
            raise RuntimeError(f"Gerekli girdi bulunamadı: {path.name}")

    micro = json.loads(MICRO_BLIND_JSON.read_text(encoding="utf-8"))
    coverage = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))
    report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    result = analyze_current_scene(micro, coverage, report)

    OUTPUT_JSON.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Gülbahçe gerçek güncel-sahne körlük: "
        f"durum={result['durum']}, "
        f"mikro-150-249={result['guncel_sahne_mikro_kor_150_249']}, "
        f"250-6500={result['guncel_sahne_kor_250_6500']}, "
        f"ikili-sahne-250+={result['ikili_sahne_korlugu_250plus_diagnostik']}, "
        f"su/unknown={result['tarihsel_su_kalite_kor_150plus']}/"
        f"{result['cozulmemis_yuzey_kalite_kor_150plus']}. "
        "Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
