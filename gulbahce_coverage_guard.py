"""Gülbahçe için üretim Sentinel kutusunun mekânsal kapsamasını ayrı ölçer.

Bu katman alarm veya saha görevi üretmez. Gülbahçe merkezinin ve çevresindeki
2 km operasyonel diagnostik çemberinin mevcut ``uzunkuyu`` üretim kutusunda ne
kadar kaldığını ölçer. 2 km çember idari/kadastral mahalle sınırı değildir;
yalnız kör-alan erken uyarısı için sabit bir operasyon tamponudur.

Referans merkez: Gülbahçe, Urla için 38.33278 N / 26.64556 E. Koordinat yalnız
kapsama denetimi içindir; ada/parsel veya hukuki sınır olarak kullanılmaz.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path

from daily_report import ISTANBUL
from satellite import REGIONS, _nearest_place, _output_shape


OUTPUT_FILE = Path(__file__).with_name("gulbahce_coverage_guard.json")
REGION_KEY = "uzunkuyu"
GULBAHCE_LAT = 38.33278
GULBAHCE_LON = 26.64556
MONITOR_RADIUS_M = 2_000
COVERAGE_OK_PERCENT = 99.5
INTEGRATION_SLICES = 2_000


def _meters_per_degree_lon(latitude: float) -> float:
    return 111_320.0 * math.cos(math.radians(latitude))


def _local_bbox_meters(bbox, latitude, longitude):
    west, south, east, north = map(float, bbox)
    lon_scale = _meters_per_degree_lon(latitude)
    return {
        "west": (west - longitude) * lon_scale,
        "east": (east - longitude) * lon_scale,
        "south": (south - latitude) * 110_570.0,
        "north": (north - latitude) * 110_570.0,
    }


def _circle_coverage_fraction(bbox, latitude, longitude, radius_m, slices=INTEGRATION_SLICES):
    """Yerel metre düzleminde çemberin bbox içinde kalan alan oranını hesaplar."""
    radius = float(radius_m)
    if radius <= 0:
        raise ValueError("radius_m pozitif olmalı")
    local = _local_bbox_meters(bbox, latitude, longitude)
    slices = max(int(slices), 200)
    dy = 2.0 * radius / slices
    total_width = 0.0
    covered_width = 0.0

    for index in range(slices):
        y = -radius + (index + 0.5) * dy
        half_width = math.sqrt(max(radius * radius - y * y, 0.0))
        full_width = 2.0 * half_width
        total_width += full_width
        if y < local["south"] or y > local["north"]:
            continue
        left = max(-half_width, local["west"])
        right = min(half_width, local["east"])
        if right > left:
            covered_width += right - left

    if total_width <= 0:
        return 0.0
    return covered_width / total_width


def _pixel_metrics(bbox):
    height, width = _output_shape(bbox)
    west, south, east, north = map(float, bbox)
    mean_lat = (south + north) / 2.0
    pixel_width_m = (
        (east - west) * 111_320.0 * math.cos(math.radians(mean_lat)) / width
    )
    pixel_height_m = (north - south) * 110_570.0 / height
    return {
        "height": int(height),
        "width": int(width),
        "pixel_width_m": round(pixel_width_m, 2),
        "pixel_height_m": round(pixel_height_m, 2),
        "pixel_edge_max_m": round(max(pixel_width_m, pixel_height_m), 2),
    }


def _core_payload():
    region = REGIONS[REGION_KEY]
    bbox = list(map(float, region["bbox"]))
    local = _local_bbox_meters(bbox, GULBAHCE_LAT, GULBAHCE_LON)
    coverage_fraction = _circle_coverage_fraction(
        bbox,
        GULBAHCE_LAT,
        GULBAHCE_LON,
        MONITOR_RADIUS_M,
    )
    coverage_percent = coverage_fraction * 100.0
    center_inside = (
        bbox[0] <= GULBAHCE_LON <= bbox[2]
        and bbox[1] <= GULBAHCE_LAT <= bbox[3]
    )
    full_radius_inside = min(
        local["east"], -local["west"], local["north"], -local["south"]
    ) >= MONITOR_RADIUS_M
    current_label = _nearest_place(GULBAHCE_LAT, GULBAHCE_LON)
    label_explicit = current_label == "Gülbahçe"
    pixel = _pixel_metrics(bbox)

    issues = []
    if not center_inside:
        issues.append("GULBAHCE_MERKEZI_URETIM_KUTUSU_DISINDA")
    if coverage_percent < COVERAGE_OK_PERCENT:
        issues.append("GULBAHCE_2KM_TAMPONU_TAM_KAPSANMIYOR")
    if not label_explicit:
        issues.append("GULBAHCE_MAHALLE_ETIKETI_ACIK_DEGIL")
    if pixel["pixel_edge_max_m"] > 10.5:
        issues.append("ANALIZ_COZUNURLUGU_10M_SINIFINDAN_UZAKLASIYOR")

    return {
        "amac": (
            "Gülbahçe merkezli 2 km operasyonel diagnostik tamponun mevcut Sentinel "
            "üretim kutusundaki geometrik kapsamasını ve mahalle etiketleme görünürlüğünü "
            "ölçmek; alarm veya saha görevi üretmez."
        ),
        "referans": {
            "ad": "Gülbahçe, Urla",
            "enlem": GULBAHCE_LAT,
            "boylam": GULBAHCE_LON,
            "not": "Operasyonel referans noktasıdır; idari/kadastral sınır veya ada-parsel değildir.",
        },
        "operasyon_tampon_m": MONITOR_RADIUS_M,
        "uretim_bolgesi": REGION_KEY,
        "uretim_bolge_etiketi": region["label"],
        "uretim_bbox": bbox,
        "merkez_bbox_icinde": center_inside,
        "tampon_tamamen_bbox_icinde": full_radius_inside,
        "tampon_kapsama_yuzde": round(coverage_percent, 2),
        "tampon_disinda_yuzde": round(max(0.0, 100.0 - coverage_percent), 2),
        "kenar_mesafeleri_m": {
            "bati": round(-local["west"]),
            "dogu": round(local["east"]),
            "guney": round(-local["south"]),
            "kuzey": round(local["north"]),
        },
        "mevcut_mahalle_etiketi": current_label,
        "gulbahce_etiketi_acik": label_explicit,
        "analiz_grid": pixel,
        "durum": "ok" if not issues else "dikkat_gerekiyor",
        "sorunlar": issues,
    }


def _write_if_changed(core):
    previous_core = None
    if OUTPUT_FILE.exists():
        try:
            previous = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
            if isinstance(previous, dict):
                previous_core = {
                    key: value for key, value in previous.items() if key != "olusturma"
                }
        except (OSError, ValueError, json.JSONDecodeError):
            previous_core = None

    if previous_core == core:
        print("Gülbahçe kapsama denetimi değişmedi; JSON yeniden yazılmadı.")
        return False

    payload = {
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        **core,
    }
    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def _self_check():
    full = [-0.1, -0.1, 0.1, 0.1]
    assert _circle_coverage_fraction(full, 0.0, 0.0, 1_000) > 0.999

    half = [-0.1, -0.1, 0.0, 0.1]
    half_fraction = _circle_coverage_fraction(half, 0.0, 0.0, 1_000)
    assert 0.495 <= half_fraction <= 0.505, half_fraction

    region_bbox = REGIONS[REGION_KEY]["bbox"]
    assert region_bbox[0] <= GULBAHCE_LON <= region_bbox[2]
    assert region_bbox[1] <= GULBAHCE_LAT <= region_bbox[3]

    pixel = _pixel_metrics(region_bbox)
    assert pixel["pixel_edge_max_m"] <= 10.5, pixel
    print("gulbahce_coverage_guard self-check: OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        _self_check()
        return

    core = _core_payload()
    _write_if_changed(core)
    print(
        "Gülbahçe kapsama: "
        f"2 km tampon %{core['tampon_kapsama_yuzde']:.2f} içeride; "
        f"doğu kenar mesafesi {core['kenar_mesafeleri_m']['dogu']} m; "
        f"etiket={core['mevcut_mahalle_etiketi']}; durum={core['durum']}"
    )


if __name__ == "__main__":
    main()
