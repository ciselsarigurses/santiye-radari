"""Gülbahçe'de tarihsel kara -> güncel SCL=su geçişlerini kıyıdan ayırır.

Sentinel SCL=6 üretim açısından su kabul edilir ve ana değişim maskesine girmez. Bu
normalde doğru ve güvenli bir davranıştır; ancak bağımsız geçmiş sahnelerde kara
(SCL=4/5) olduğu kanıtlanan küçük/kompakt bir alanın tek güncel sahnede SCL=6
olması, özellikle kıyıdan uzakta ise, yalnız su/kıyı diye tamamen unutulmamalıdır.

Bu denetim yalnız diagnostiktir: alarm veya saha görevi üretmez, 250 m² ana üretim
eşiğini değiştirmez ve 150-249 m² MİKRO katmanını otomatik yükseltmez. Tarihsel
suya yakın geçişleri KIYI_SU_ARKA_PLAN olarak tutar; tarihsel sudan uzakta kalan
kompakt geçişleri IC_KARA_SCL_SU_BELIRSIZLIGI olarak bir sonraki açık Sentinel
sahnesinde yeniden ölçülmek üzere arka planda saklar. 6.500 m² üstü geniş kümeler
ayrıca GENIS_SU_YUZEY_ARKA_PLAN sınıfında kalır.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

import satellite
from coverage_blind_area_audit import _historical_surface_mask, _pixel_area_m2
from gulbahce_latest_state_blind_review import _independent_historical_items
from micro_site_audit import (
    GULBAHCE_OPERATION_POINT,
    GULBAHCE_OPERATION_RADIUS_M,
    _circle_mask,
    _representative_point,
)


OUTPUT_JSON = Path(__file__).with_name("gulbahce_land_water_transition_audit.json")
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
MAX_LOCAL_AREA_M2 = 6500
HISTORICAL_WATER_NEAR_RADIUS_PIXELS = 2
COASTAL_NEAR_SHARE_MIN = 0.50
COMPACT_FILL_MIN = 0.50
WATER_CLASS = 6


def _dilate(mask, radius):
    """Küçük yarıçaplı kare komşuluk genişletmesi; scipy gerektirmez."""
    source = np.asarray(mask, dtype=bool)
    if radius <= 0:
        return source.copy()
    height, width = source.shape
    padded = np.pad(source, int(radius), mode="constant", constant_values=False)
    out = np.zeros_like(source, dtype=bool)
    span = int(radius) * 2 + 1
    for row_offset in range(span):
        for col_offset in range(span):
            out |= padded[
                row_offset:row_offset + height,
                col_offset:col_offset + width,
            ]
    return out


def _component_geometry(component):
    pixels = np.asarray(component, dtype="int32")
    row_span = int(pixels[:, 0].max() - pixels[:, 0].min() + 1)
    col_span = int(pixels[:, 1].max() - pixels[:, 1].min() + 1)
    bbox_pixels = max(row_span * col_span, 1)
    return row_span, col_span, bbox_pixels, float(len(component) / bbox_pixels)


def _classify_components(mask, known_water, bbox, pixel_area):
    near_historical_water = _dilate(
        known_water,
        HISTORICAL_WATER_NEAR_RADIUS_PIXELS,
    )
    rows = []
    for component in satellite._connected_components(mask):
        area_m2 = float(len(component) * pixel_area)
        if area_m2 < MICRO_RANGE_M2[0]:
            continue

        pixels = np.asarray(component, dtype="int32")
        near_share = float(
            np.mean(near_historical_water[pixels[:, 0], pixels[:, 1]])
        )
        row_span, col_span, bbox_pixels, fill = _component_geometry(component)
        latitude, longitude = _representative_point(component, bbox, mask.shape)

        if area_m2 > MAX_LOCAL_AREA_M2:
            surface_class = "GENIS_SU_YUZEY_ARKA_PLAN"
            reimage = False
        elif near_share >= COASTAL_NEAR_SHARE_MIN:
            surface_class = "KIYI_SU_ARKA_PLAN"
            reimage = False
        else:
            surface_class = "IC_KARA_SCL_SU_BELIRSIZLIGI"
            reimage = fill >= COMPACT_FILL_MIN

        rows.append(
            {
                "enlem": latitude,
                "boylam": longitude,
                "alan_m2": int(round(area_m2)),
                "piksel": len(component),
                "bbox_piksel": bbox_pixels,
                "satir_genisligi_piksel": row_span,
                "sutun_genisligi_piksel": col_span,
                "doluluk_orani": round(fill, 3),
                "tarihsel_suya_yakin_piksel_orani": round(near_share, 3),
                "sinif": surface_class,
                "yeniden_goruntule": bool(reimage),
                "alarm": False,
                "saha_gorevi": False,
                "not": (
                    "Bu SCL su sınıfı inşaat kanıtı değildir. Tarihsel kara kanıtı "
                    "ile güncel SCL=6 çelişkisi yalnız kıyıdan ayrıştırılarak arka "
                    "planda tutulur; sonraki açık Sentinel sahnesinde yeniden ölçülür."
                ),
            }
        )

    rows.sort(
        key=lambda item: (
            item["sinif"] != "IC_KARA_SCL_SU_BELIRSIZLIGI",
            -int(item["yeniden_goruntule"]),
            item["alan_m2"],
            item["enlem"],
            item["boylam"],
        )
    )
    return rows


def build_audit_from_masks(
    *,
    bbox,
    latest_scl,
    known_land,
    known_water,
    operation,
    pixel_area,
    source_date,
    source_item,
    reference_dates,
):
    if latest_scl.shape != known_land.shape or latest_scl.shape != known_water.shape:
        raise ValueError("SCL ve tarihsel yüzey maskeleri aynı boyutta olmalı.")

    historical_land_current_water = (
        np.asarray(operation, dtype=bool)
        & np.asarray(known_land, dtype=bool)
        & (np.asarray(latest_scl) == WATER_CLASS)
    )
    rows = _classify_components(
        historical_land_current_water,
        known_water,
        bbox,
        pixel_area,
    )

    inland = [row for row in rows if row["sinif"] == "IC_KARA_SCL_SU_BELIRSIZLIGI"]
    coastal = [row for row in rows if row["sinif"] == "KIYI_SU_ARKA_PLAN"]
    wide = [row for row in rows if row["sinif"] == "GENIS_SU_YUZEY_ARKA_PLAN"]
    reimage = [row for row in inland if row["yeniden_goruntule"]]
    micro_reimage = [
        row for row in reimage
        if MICRO_RANGE_M2[0] <= row["alan_m2"] <= MICRO_RANGE_M2[1]
    ]
    main_reimage = [
        row for row in reimage
        if MAIN_THRESHOLD_M2 <= row["alan_m2"] <= MAX_LOCAL_AREA_M2
    ]

    return {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_santiye_araligi_m2": MICRO_RANGE_M2,
        "kaynak_son_tarih": source_date or None,
        "kaynak_son_item": source_item or None,
        "kara_referans_sahne_sayisi": len(reference_dates or []),
        "kara_referans_tarihleri": list(reference_dates or []),
        "tarihsel_su_yakinlik_yaricapi_piksel": HISTORICAL_WATER_NEAR_RADIUS_PIXELS,
        "kiyi_sinifi_min_yakin_piksel_orani": COASTAL_NEAR_SHARE_MIN,
        "kompakt_min_doluluk_orani": COMPACT_FILL_MIN,
        "tarihsel_kara_guncel_su_150plus": len(rows),
        "kiyi_su_arka_plan": len(coastal),
        "ic_kara_scl_su_belirsizligi": len(inland),
        "genis_su_yuzey_arka_plan": len(wide),
        "yeniden_goruntule_kompakt": len(reimage),
        "yeniden_goruntule_mikro_150_249": len(micro_reimage),
        "yeniden_goruntule_ana_250_6500": len(main_reimage),
        "adaylar": rows,
        "yorum": (
            "Bu katman alarm/görev üretmez ve SCL=su piksellerini şantiye saymaz. "
            "Ama tarihsel kara olan küçük/kompakt SCL=su geçişlerini sırf su etiketi "
            "nedeniyle kaybetmez: tarihsel suya yakınsa kıyı arka planında, uzaktaysa "
            "SCL belirsizliği olarak sonraki açık sahneye taşır."
        ),
    }


def analyze_current_scene():
    bbox = satellite.REGIONS["uzunkuyu"]["bbox"]
    _older, latest = satellite.sentinel_pair("uzunkuyu")
    height, width = satellite._output_shape(bbox)
    latest_scl = satellite._read_asset(
        latest,
        "scl",
        bbox,
        height,
        width,
        "nearest",
    )[0]
    pixel_area = _pixel_area_m2(bbox, height, width)
    operation = _circle_mask(
        bbox,
        latest_scl.shape,
        GULBAHCE_OPERATION_POINT,
        GULBAHCE_OPERATION_RADIUS_M,
    )

    items = _independent_historical_items(satellite._search_items(bbox), latest)
    known_land, known_water, _unknown, refs, ref_dates = _historical_surface_mask(
        items,
        latest,
        bbox,
        height,
        width,
    )
    if not refs:
        raise RuntimeError("Gülbahçe kara-su geçişi için bağımsız tarihsel referans yok.")

    return build_audit_from_masks(
        bbox=bbox,
        latest_scl=latest_scl,
        known_land=known_land,
        known_water=known_water,
        operation=operation,
        pixel_area=pixel_area,
        source_date=satellite._item_date(latest),
        source_item=str(latest.get("id") or ""),
        reference_dates=ref_dates,
    )


def _self_check():
    assert satellite.MIN_HOTSPOT_AREA_M2 == MAIN_THRESHOLD_M2
    bbox = (26.64, 38.32, 26.65, 38.33)
    shape = (10, 10)
    latest_scl = np.full(shape, 4, dtype="uint8")
    known_land = np.zeros(shape, dtype=bool)
    known_water = np.zeros(shape, dtype=bool)
    operation = np.ones(shape, dtype=bool)

    # Tarihsel suya komşu 300 m² geçiş kıyı arka planında kalmalı.
    known_water[0, 1:4] = True
    known_land[1, 1:4] = True
    latest_scl[1, 1:4] = WATER_CLASS

    # Kıyıdan uzak kompakt 300 m² geçiş yeniden görüntüleme belirsizliği olmalı.
    known_land[7, 6:9] = True
    latest_scl[7, 6:9] = WATER_CLASS

    # Kıyıdan uzak kompakt 200 m² geçiş MİKRO alarmına dönüşmeden saklanmalı.
    known_land[4, 6:8] = True
    latest_scl[4, 6:8] = WATER_CLASS

    result = build_audit_from_masks(
        bbox=bbox,
        latest_scl=latest_scl,
        known_land=known_land,
        known_water=known_water,
        operation=operation,
        pixel_area=100.0,
        source_date="08.09.2026",
        source_item="S2_TEST",
        reference_dates=["05.09.2026", "03.09.2026"],
    )
    assert result["tarihsel_kara_guncel_su_150plus"] == 3
    assert result["kiyi_su_arka_plan"] == 1
    assert result["ic_kara_scl_su_belirsizligi"] == 2
    assert result["yeniden_goruntule_mikro_150_249"] == 1
    assert result["yeniden_goruntule_ana_250_6500"] == 1
    assert result["alarm"] is False and result["saha_gorevi"] is False
    assert all(item["alarm"] is False for item in result["adaylar"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if args.check_only:
        print(
            "Gülbahçe kara-su geçiş öz testi başarılı; kıyı ve iç-kara SCL su "
            "belirsizliği ayrıldı, 250 m² eşik ve MİKRO alarm politikası değişmedi."
        )
        return

    result = analyze_current_scene()
    OUTPUT_JSON.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Gülbahçe kara-su geçiş diagnostiği: "
        f"toplam={result['tarihsel_kara_guncel_su_150plus']}, "
        f"kıyı={result['kiyi_su_arka_plan']}, "
        f"iç-kara-belirsiz={result['ic_kara_scl_su_belirsizligi']}, "
        f"yeniden-görüntüle={result['yeniden_goruntule_kompakt']}. "
        "Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
