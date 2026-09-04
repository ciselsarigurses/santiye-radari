"""Gülbahçe için üretim ve birleşik Sentinel kutularının mekânsal kapsamasını ölçer.

Bu katman alarm veya saha görevi üretmez. Gülbahçe merkezinin ve çevresindeki
2 km operasyonel diagnostik çemberinin mevcut ``uzunkuyu`` üretim kutusunda ne
kadar kaldığını ölçer. Ayrıca ``all`` birleşik zarfının gerçekten Çeşme ve
Uzunkuyu üretim kutularının tamamını içerip içermediğini ayrı bir bütünlük
kontrolü olarak raporlar. 2 km çember idari/kadastral mahalle sınırı değildir;
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
ALL_REGION_KEY = "all"
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


def _bbox_union(*bboxes):
    """Bir veya daha fazla WGS84 bbox için en küçük kapsayıcı zarfı döndürür."""
    if not bboxes:
        raise ValueError("En az bir bbox gerekli")
    normalized = [list(map(float, bbox)) for bbox in bboxes]
    return [
        min(bbox[0] for bbox in normalized),
        min(bbox[1] for bbox in normalized),
        max(bbox[2] for bbox in normalized),
        max(bbox[3] for bbox in normalized),
    ]


def _bbox_contains(outer, inner, tolerance=1e-9):
    """Outer zarfın inner zarfı bütünüyle içerip içermediğini ölçer."""
    outer = list(map(float, outer))
    inner = list(map(float, inner))
    tolerance = max(float(tolerance), 0.0)
    return (
        outer[0] <= inner[0] + tolerance
        and outer[1] <= inner[1] + tolerance
        and outer[2] >= inner[2] - tolerance
        and outer[3] >= inner[3] - tolerance
    )


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
    all_bbox = list(map(float, REGIONS[ALL_REGION_KEY]["bbox"]))
    cesme_bbox = list(map(float, REGIONS["cesme"]["bbox"]))
    expected_union_bbox = _bbox_union(cesme_bbox, bbox)

    local = _local_bbox_meters(bbox, GULBAHCE_LAT, GULBAHCE_LON)
    coverage_fraction = _circle_coverage_fraction(
        bbox,
        GULBAHCE_LAT,
        GULBAHCE_LON,
        MONITOR_RADIUS_M,
    )
    coverage_percent = coverage_fraction * 100.0
    all_coverage_percent = _circle_coverage_fraction(
        all_bbox,
        GULBAHCE_LAT,
        GULBAHCE_LON,
        MONITOR_RADIUS_M,
    ) * 100.0
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

    production_issues = []
    if not center_inside:
        production_issues.append("GULBAHCE_MERKEZI_URETIM_KUTUSU_DISINDA")
    if coverage_percent < COVERAGE_OK_PERCENT:
        production_issues.append("GULBAHCE_2KM_TAMPONU_TAM_KAPSANMIYOR")
    if not label_explicit:
        production_issues.append("GULBAHCE_MAHALLE_ETIKETI_ACIK_DEGIL")
    if pixel["pixel_edge_max_m"] > 10.5:
        production_issues.append("ANALIZ_COZUNURLUGU_10M_SINIFINDAN_UZAKLASIYOR")

    union_issues = []
    all_contains_union = _bbox_contains(all_bbox, expected_union_bbox)
    if not all_contains_union:
        union_issues.append("BIRLESIK_ALL_ZARFI_URETIM_BOLGELERINI_TAM_KAPSAMIYOR")
    if all_coverage_percent < COVERAGE_OK_PERCENT:
        union_issues.append("BIRLESIK_ALL_ZARFI_GULBAHCE_2KM_TAMPONUNU_TAM_KAPSAMIYOR")

    issues = production_issues + union_issues
    return {
        "amac": (
            "Gülbahçe merkezli 2 km operasyonel diagnostik tamponun mevcut Sentinel "
            "üretim kutusundaki geometrik kapsamasını ve mahalle etiketleme görünürlüğünü "
            "ölçmek; ayrıca birleşik all zarfının iki üretim bölgesini eksiksiz içerdiğini "
            "doğrulamak. Alarm veya saha görevi üretmez."
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
        "uretim_durumu": "ok" if not production_issues else "dikkat_gerekiyor",
        "uretim_sorunlari": production_issues,
        "birlesik_all_denetime": {
            "mevcut_bbox": all_bbox,
            "beklenen_uretim_birlesimi_bbox": expected_union_bbox,
            "uretim_birlesimini_tam_kapsiyor": all_contains_union,
            "gulbahce_2km_tampon_kapsama_yuzde": round(all_coverage_percent, 2),
            "durum": "ok" if not union_issues else "dikkat_gerekiyor",
            "sorunlar": union_issues,
            "not": (
                "all zarfı doğrudan saha alarmı değildir; ancak birleşik/diagnostik taramalar "
                "gelecekte bu anahtarı kullandığında Gülbahçe doğu kenarında sessiz körlük "
                "oluşmaması için üretim kutularının birleşimini eksiksiz içermelidir."
            ),
        },
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

    union = _bbox_union([0.0, 0.0, 1.0, 1.0], [0.5, -1.0, 2.0, 0.5])
    assert union == [0.0, -1.0, 2.0, 1.0], union
    assert _bbox_contains([-1.0, -2.0, 3.0, 2.0], union)
    assert not _bbox_contains([0.0, -1.0, 1.9, 1.0], union)

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
    union = core["birlesik_all_denetime"]
    print(
        "Gülbahçe kapsama: "
        f"üretim 2 km tampon %{core['tampon_kapsama_yuzde']:.2f} içeride; "
        f"doğu kenar mesafesi {core['kenar_mesafeleri_m']['dogu']} m; "
        f"all tampon %{union['gulbahce_2km_tampon_kapsama_yuzde']:.2f}; "
        f"etiket={core['mevcut_mahalle_etiketi']}; durum={core['durum']}"
    )


if __name__ == "__main__":
    main()
