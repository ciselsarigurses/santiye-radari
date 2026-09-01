"""Küçük kuru-zemin adayında değişimin çevresine göre ne kadar yerel olduğunu ölçer.

Yeni hafriyat/temel başlangıcı özellikle 250-900 m² aralığında birkaç Sentinel pikselinde
yoğunlaşabilir. Tarla sürümü, geniş kuru-zemin hazırlığı veya bölgesel görüntü farkı ise
aynı anda adayın hemen çevresinde de güçlü değişim bırakabilir. Bu diagnostik katman,
mevcut 3x3 temporal yamanın dışındaki 5x5 halka ile güncel ve önceki BSI değişimini
karşılaştırır.

Alarm, görev, 250 m² eşiği veya üretim sınıflaması değiştirilmez. Çıktı yalnız saha
kalibrasyonu ve yanlış-pozitif analizi içindir; saha geri bildirimi olmadan yeni şantiye
kabul edilmez.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np

import dry_ground_temporal_audit as temporal
import satellite
from daily_report import ISTANBUL


SOURCE_AUDIT = Path(__file__).with_name("dry_ground_temporal_audit.json")
OUTPUT_AUDIT = Path(__file__).with_name("temporal_locality_audit.json")
MAX_LOCAL_SITE_AREA_M2 = 900
OUTER_RADIUS_PIXELS = 2
MIN_RING_VALID_FRACTION = 2 / 3
LOCAL_CONTRAST_MIN = 1.5
WIDESPREAD_RING_ABS_MIN = 0.10
WIDESPREAD_RING_RELATIVE_MIN = 0.80


def _ring_means(previous_delta, current_delta, valid_mask, row, column):
    """5x5 pencerenin merkez 3x3 dışındaki halkasını aynı geçerli piksellerle ölç."""
    height, width = valid_mask.shape
    r0 = max(0, row - OUTER_RADIUS_PIXELS)
    r1 = min(height, row + OUTER_RADIUS_PIXELS + 1)
    c0 = max(0, column - OUTER_RADIUS_PIXELS)
    c1 = min(width, column + OUTER_RADIUS_PIXELS + 1)

    ring = np.ones((r1 - r0, c1 - c0), dtype=bool)
    inner_r0 = max(row - 1, r0) - r0
    inner_r1 = min(row + 2, r1) - r0
    inner_c0 = max(column - 1, c0) - c0
    inner_c1 = min(column + 2, c1) - c0
    ring[inner_r0:inner_r1, inner_c0:inner_c1] = False

    total_ring = int(ring.sum())
    if total_ring <= 0:
        return None, None, 0.0

    local_valid = valid_mask[r0:r1, c0:c1] & ring
    valid_count = int(local_valid.sum())
    valid_fraction = valid_count / total_ring
    if valid_count <= 0:
        return None, None, valid_fraction

    previous_patch = previous_delta[r0:r1, c0:c1]
    current_patch = current_delta[r0:r1, c0:c1]
    return (
        float(np.mean(previous_patch[local_valid])),
        float(np.mean(current_patch[local_valid])),
        valid_fraction,
    )


def _locality_flags(inner_current, ring_current, ring_valid_fraction, abrupt, area_m2):
    if inner_current is None or ring_current is None:
        return None, False, False
    inner = abs(float(inner_current))
    ring = abs(float(ring_current))
    valid = float(ring_valid_fraction or 0)
    contrast = inner / max(ring, 0.01)
    eligible = float(area_m2 or 0) <= MAX_LOCAL_SITE_AREA_M2
    local_support = bool(
        eligible
        and abrupt
        and valid >= MIN_RING_VALID_FRACTION
        and inner >= 0.10
        and contrast >= LOCAL_CONTRAST_MIN
    )
    widespread_risk = bool(
        eligible
        and valid >= MIN_RING_VALID_FRACTION
        and ring >= max(WIDESPREAD_RING_ABS_MIN, inner * WIDESPREAD_RING_RELATIVE_MIN)
    )
    return round(contrast, 2), local_support, widespread_risk


def _analyze_region(region_key, region_data):
    if region_data.get("durum") != "ok":
        return {"durum": "atlandi", "neden": "temporal_bolge_hazir_degil"}

    bbox = satellite.REGIONS[region_key]["bbox"]
    items = satellite._search_items(bbox)
    previous = temporal._find_item(items, region_data.get("degisim_oncesi_item"))
    older = temporal._find_item(items, region_data.get("onceki_item"))
    latest = temporal._find_item(items, region_data.get("son_item"))
    if previous is None or older is None or latest is None:
        return {
            "durum": "atlandi",
            "neden": "temporal_sahnelerden_biri_bulunamadi",
        }

    height, width = satellite._output_shape(bbox)
    previous_bsi, previous_scl = temporal._bsi_for_item(previous, bbox, height, width)
    older_bsi, older_scl = temporal._bsi_for_item(older, bbox, height, width)
    latest_bsi, latest_scl = temporal._bsi_for_item(latest, bbox, height, width)
    previous_delta = np.abs(older_bsi - previous_bsi)
    current_delta = np.abs(latest_bsi - older_bsi)

    valid = ~np.isin(previous_scl, satellite.EXCLUDED_SCL_CLASSES)
    valid &= ~np.isin(older_scl, satellite.EXCLUDED_SCL_CLASSES)
    valid &= ~np.isin(latest_scl, satellite.EXCLUDED_SCL_CLASSES)

    rows = []
    for raw in region_data.get("adaylar") or []:
        if not isinstance(raw, dict):
            continue
        try:
            area_m2 = float(raw.get("alan_m2") or 0)
            latitude = float(raw.get("enlem"))
            longitude = float(raw.get("boylam"))
        except (TypeError, ValueError):
            continue
        if not (250 <= area_m2 <= MAX_LOCAL_SITE_AREA_M2):
            continue

        row, column = temporal._pixel_for_point(
            latitude, longitude, bbox, previous_delta.shape
        )
        row_slice, col_slice = temporal._patch_slices(
            row, column, previous_delta.shape
        )
        _, inner_current, inner_valid_fraction = temporal._paired_patch_means(
            previous_delta,
            current_delta,
            valid,
            row_slice,
            col_slice,
        )
        ring_previous, ring_current, ring_valid_fraction = _ring_means(
            previous_delta,
            current_delta,
            valid,
            row,
            column,
        )
        abrupt = bool(raw.get("ani_baslangic_destegi"))
        contrast, local_support, widespread_risk = _locality_flags(
            inner_current,
            ring_current,
            ring_valid_fraction,
            abrupt,
            area_m2,
        )
        stored_inner = raw.get("son_cift_bsi_degisim")
        stored_delta = (
            abs(float(stored_inner) - float(inner_current))
            if stored_inner is not None and inner_current is not None
            else None
        )

        rows.append(
            {
                "mahalle": raw.get("mahalle"),
                "enlem": round(latitude, 6),
                "boylam": round(longitude, 6),
                "alan_m2": round(area_m2),
                "ani_baslangic_destegi": abrupt,
                "ic_3x3_son_bsi_degisim": round(inner_current, 4) if inner_current is not None else None,
                "ic_3x3_gecerli_oran": round(inner_valid_fraction, 3),
                "cevre_halka_onceki_bsi_degisim": round(ring_previous, 4) if ring_previous is not None else None,
                "cevre_halka_son_bsi_degisim": round(ring_current, 4) if ring_current is not None else None,
                "cevre_halka_gecerli_oran": round(ring_valid_fraction, 3),
                "yerellik_orani": contrast,
                "lokal_ani_baslangic_destegi": local_support,
                "yaygin_cevre_degisim_riski": widespread_risk,
                "kayitli_3x3_farki": round(stored_delta, 6) if stored_delta is not None else None,
            }
        )

    rows.sort(
        key=lambda item: (
            not bool(item.get("lokal_ani_baslangic_destegi")),
            bool(item.get("yaygin_cevre_degisim_riski")),
            -float(item.get("yerellik_orani") or 0),
            -float(item.get("ic_3x3_son_bsi_degisim") or 0),
        )
    )
    abrupt_rows = [item for item in rows if item.get("ani_baslangic_destegi")]
    return {
        "durum": "ok",
        "bolge": satellite.REGIONS[region_key]["label"],
        "degisim_oncesi_item": previous.get("id"),
        "onceki_item": older.get("id"),
        "son_item": latest.get("id"),
        "kucuk_orta_aday": len(rows),
        "ani_baslangic_adayi": len(abrupt_rows),
        "lokal_ani_baslangic_destegi": sum(
            1 for item in rows if item.get("lokal_ani_baslangic_destegi")
        ),
        "yaygin_cevre_degisim_riski": sum(
            1 for item in rows if item.get("yaygin_cevre_degisim_riski")
        ),
        "ani_adaylarda_lokal_destek": sum(
            1 for item in abrupt_rows if item.get("lokal_ani_baslangic_destegi")
        ),
        "ani_adaylarda_yaygin_cevre_riski": sum(
            1 for item in abrupt_rows if item.get("yaygin_cevre_degisim_riski")
        ),
        "adaylar": rows,
    }


def _self_check():
    previous = np.zeros((5, 5), dtype="float32") + 0.01
    current = np.zeros((5, 5), dtype="float32") + 0.02
    current[1:4, 1:4] = 0.20
    valid = np.ones((5, 5), dtype=bool)
    prev_ring, current_ring, valid_fraction = _ring_means(
        previous, current, valid, 2, 2
    )
    assert abs(float(prev_ring) - 0.01) < 1e-6
    assert abs(float(current_ring) - 0.02) < 1e-6
    assert valid_fraction == 1.0
    contrast, local_support, widespread = _locality_flags(
        0.20, current_ring, valid_fraction, True, 500
    )
    assert contrast >= 9.0
    assert local_support
    assert not widespread

    broad = np.zeros((5, 5), dtype="float32") + 0.15
    _, broad_ring, broad_valid = _ring_means(previous, broad, valid, 2, 2)
    contrast, local_support, widespread = _locality_flags(
        0.16, broad_ring, broad_valid, True, 500
    )
    assert not local_support
    assert widespread

    # 900 m² üstünü bu küçük-saha yerellik sınıflamasına sokma.
    _, local_support, widespread = _locality_flags(0.20, 0.02, 1.0, True, 1200)
    assert not local_support
    assert not widespread


def run_audit():
    _self_check()
    if not SOURCE_AUDIT.exists():
        raise RuntimeError("dry_ground_temporal_audit.json bulunamadı.")
    source = json.loads(SOURCE_AUDIT.read_text(encoding="utf-8"))
    payload = {
        "rapor_tarihi": datetime.now(ISTANBUL).strftime("%Y-%m-%d"),
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "amac": (
            "250-900 m² temporal kuru-zemin adayında 3x3 değişimin hemen dışındaki "
            "5x5 halkaya göre yerel mi yaygın mı olduğunu ölçmek."
        ),
        "esikler": {
            "kucuk_orta_maks_alan_m2": MAX_LOCAL_SITE_AREA_M2,
            "ic_yama": "3x3",
            "cevre_penceresi": "5x5_dis_halka",
            "minimum_halka_gecerli_oran": MIN_RING_VALID_FRACTION,
            "lokal_kontrast_min": LOCAL_CONTRAST_MIN,
            "yaygin_halka_mutlak_bsi_min": WIDESPREAD_RING_ABS_MIN,
            "yaygin_halka_goreli_min": WIDESPREAD_RING_RELATIVE_MIN,
        },
        "uyari": (
            "Diagnostiktir; alarm/görev/eşik değiştirmez. Yerel veya yaygın etiketi "
            "saha doğrulaması olmadan şantiye/yanlış pozitif kararı değildir."
        ),
        "bolgeler": {},
    }
    for region_key, region_data in (source.get("bolgeler") or {}).items():
        if region_key not in satellite.REGIONS or not isinstance(region_data, dict):
            continue
        payload["bolgeler"][region_key] = _analyze_region(region_key, region_data)

    OUTPUT_AUDIT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = []
    for key, region in payload["bolgeler"].items():
        if region.get("durum") == "ok":
            summary.append(
                f"{key}={region.get('kucuk_orta_aday', 0)} "
                f"(ani={region.get('ani_baslangic_adayi', 0)}, "
                f"lokal={region.get('ani_adaylarda_lokal_destek', 0)}, "
                f"yaygin={region.get('ani_adaylarda_yaygin_cevre_riski', 0)})"
            )
        else:
            summary.append(f"{key}=ATLANDI")
    print(
        "Küçük saha temporal yerellik denetimi tamamlandı: "
        + ", ".join(summary)
        + ". Alarm/görev üretilmedi."
    )
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if args.check_only:
        _self_check()
        print("Küçük saha temporal yerellik öz testi başarılı.")
        return 0
    run_audit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
