"""Şantiye Radarı uydu motorunun kritik mekânsal varsayımlarını doğrular.

Bu test gerçek Sentinel servisine bağlanmaz. Günlük taramanın temel hedeflerini
korur: Çeşme + Uzunkuyu kapsamasında boşluk oluşmaması, analizin yaklaşık 10 m
piksel ölçeğinde kalması, 250 m² sınıfındaki güçlü küçük saha sinyalinin, tam
kapsam Sentinel görüntü çiftinin ve mümkün olduğunda aynı göreli yörünge
referansının algoritma değişikliklerinde bozulmaması.
"""

from __future__ import annotations

import numpy as np

from satellite import (
    HOTSPOT_LIMIT,
    MAX_ANALYSIS_DIMENSION,
    MIN_HOTSPOT_AREA_M2,
    PLACE_CENTERS,
    REGIONS,
    SMALL_HOTSPOT_MIN_PIXELS,
    SMALL_HOTSPOT_QUOTA,
    TARGET_PIXEL_SIZE_M,
    SatelliteError,
    _clean_mask,
    _hotspots,
    _output_shape,
    _pick_pair,
)


REPORT_REGIONS = ("cesme", "uzunkuyu")
MAX_PIXEL_AREA_M2 = 125


def _contains(bbox, latitude, longitude):
    west, south, east, north = bbox
    return west <= longitude <= east and south <= latitude <= north


def _pixel_area_m2(bbox):
    west, south, east, north = bbox
    height, width = _output_shape(bbox)
    mean_lat = (south + north) / 2
    pixel_width_m = (
        (east - west) * 111320 * np.cos(np.radians(mean_lat)) / width
    )
    pixel_height_m = (north - south) * 110570 / height
    return float(pixel_width_m * pixel_height_m), height, width


def _fake_item(item_id, timestamp, bbox, tile, orbit=None):
    properties = {
        "datetime": timestamp,
        "s2:mgrs_tile": tile,
    }
    if orbit is not None:
        properties["sat:relative_orbit"] = orbit
    return {
        "id": item_id,
        "bbox": bbox,
        "properties": properties,
    }


def check_configuration():
    assert MIN_HOTSPOT_AREA_M2 == 250, (
        "Minimum uydu hareket eşiği 250 m² olmalı; "
        f"mevcut değer {MIN_HOTSPOT_AREA_M2}."
    )
    assert TARGET_PIXEL_SIZE_M == 10, (
        "Küçük şantiye tespiti için hedef piksel ölçeği 10 m olarak korunmalı."
    )
    assert SMALL_HOTSPOT_MIN_PIXELS <= 3, (
        "250-400 m² güçlü küçük saha adayını engelleyecek kadar yüksek "
        "minimum piksel şartı tanımlanmış."
    )
    assert HOTSPOT_LIMIT >= 24, (
        "Bölge başına aday tavanı tekrar 12 gibi düşük bir değere inmiş; "
        "yoğun dönemde gerçek 250 m²+ hareketler sessizce kesilebilir."
    )
    assert SMALL_HOTSPOT_QUOTA >= 6, (
        "Yoğun görüntüde küçük ve güçlü hafriyat adayları için ayrılan kota yetersiz."
    )

    for region_key in REPORT_REGIONS:
        pixel_area, height, width = _pixel_area_m2(REGIONS[region_key]["bbox"])
        assert height <= MAX_ANALYSIS_DIMENSION
        assert width <= MAX_ANALYSIS_DIMENSION
        assert pixel_area <= MAX_PIXEL_AREA_M2, (
            f"{region_key} analiz pikselleri yaklaşık {pixel_area:.1f} m²; "
            "250 m² küçük saha hedefi için çözünürlük fazla kaba."
        )


def check_coverage():
    report_boxes = [REGIONS[key]["bbox"] for key in REPORT_REGIONS]
    uncovered = [
        name
        for name, (latitude, longitude) in PLACE_CENTERS.items()
        if not any(_contains(box, latitude, longitude) for box in report_boxes)
    ]
    assert not uncovered, (
        "Günlük uydu taramasının dışında kalan takip merkezi var: "
        + ", ".join(uncovered)
    )

    west, south, east, north = REGIONS["all"]["bbox"]
    uncovered_grid = []
    for latitude in np.linspace(south, north, 9):
        for longitude in np.linspace(west, east, 13):
            if not any(_contains(box, latitude, longitude) for box in report_boxes):
                uncovered_grid.append((float(latitude), float(longitude)))
    assert not uncovered_grid, (
        "Günlük uydu kutularında 'all' zarfı içinde kör alan var; "
        f"ilk örnek: {uncovered_grid[0] if uncovered_grid else None}."
    )

    cesme = REGIONS["cesme"]["bbox"]
    uzunkuyu = REGIONS["uzunkuyu"]["bbox"]
    assert cesme[2] >= uzunkuyu[0], (
        "Çeşme ve Uzunkuyu günlük tarama kutuları arasında boylam boşluğu var."
    )


def check_scene_pair_coverage():
    """Kısmi karo ve farklı göreli yörünge, tercih edilen referans olmasın."""
    target = [26.25, 38.22, 26.47, 38.43]
    full = [26.20, 38.18, 26.52, 38.48]
    partial = [26.40, 38.18, 26.70, 38.48]
    items = [
        _fake_item(
            "partial-newest",
            "2026-08-28T10:00:00Z",
            partial,
            "35SNC",
            orbit=79,
        ),
        _fake_item(
            "full-latest",
            "2026-08-26T10:00:00Z",
            full,
            "35SMC",
            orbit=36,
        ),
        _fake_item(
            "full-different-orbit",
            "2026-08-24T10:00:00Z",
            full,
            "35SMC",
            orbit=79,
        ),
        _fake_item(
            "full-wrong-tile",
            "2026-08-23T10:00:00Z",
            full,
            "35SNC",
            orbit=36,
        ),
        _fake_item(
            "full-same-orbit-older",
            "2026-08-21T10:00:00Z",
            full,
            "35SMC",
            orbit=36,
        ),
    ]
    older, latest = _pick_pair(items, bbox=target)
    assert latest["id"] == "full-latest", (
        "Analiz kutusunu yalnız kısmen örten en yeni Sentinel karosu seçildi."
    )
    assert older["id"] == "full-same-orbit-older", (
        "Aynı MGRS karosunda daha yeni fakat farklı göreli yörüngedeki görüntü, "
        "aynı-yörünge referansının önüne geçti."
    )

    no_orbit_items = [
        _fake_item("latest-no-orbit", "2026-08-26T10:00:00Z", full, "35SMC"),
        _fake_item("older-no-orbit", "2026-08-24T10:00:00Z", full, "35SMC"),
    ]
    older_no_orbit, latest_no_orbit = _pick_pair(no_orbit_items, bbox=target)
    assert latest_no_orbit["id"] == "latest-no-orbit"
    assert older_no_orbit["id"] == "older-no-orbit"

    try:
        _pick_pair(
            [
                _fake_item(
                    "partial",
                    "2026-08-28T10:00:00Z",
                    partial,
                    "35SNC",
                    orbit=79,
                ),
                _fake_item(
                    "only-one-full",
                    "2026-08-25T10:00:00Z",
                    full,
                    "35SMC",
                    orbit=36,
                ),
            ],
            bbox=target,
        )
    except SatelliteError:
        pass
    else:
        raise AssertionError(
            "İki tam-kapsam görüntü yokken uydu motoru sessizce kısmi karo kabul etti."
        )


def check_small_site_path():
    signal = np.zeros((9, 9), dtype=bool)
    signal[4, 3:6] = True
    cleaned = _clean_mask(signal, small_site_mask=signal)
    hotspots = _hotspots(
        cleaned,
        [26.30, 38.28, 26.31, 38.29],
        100.0,
        small_site_mask=signal,
        limit=12,
        small_quota=3,
    )
    assert len(hotspots) == 1, (
        "Yaklaşık 300 m² güçlü küçük saha sinyali hotspot üretmiyor."
    )
    assert hotspots[0]["boyut_sinifi"] == "KUCUK"
    assert hotspots[0]["alan_m2"] == 300

    below = np.zeros((9, 9), dtype=bool)
    below[4, 3:5] = True
    cleaned_below = _clean_mask(below, small_site_mask=below)
    assert not _hotspots(
        cleaned_below,
        [26.30, 38.28, 26.31, 38.29],
        100.0,
        small_site_mask=below,
        limit=12,
        small_quota=3,
    ), "Yaklaşık 200 m² eşik-altı sinyal yanlış saha görevi üretiyor."


def check_candidate_capacity():
    signal = np.zeros((30, 8), dtype=bool)
    for index in range(14):
        row = 1 + index * 2
        signal[row, 2:5] = True
    hotspots = _hotspots(
        signal,
        [26.30, 38.28, 26.31, 38.31],
        100.0,
        small_site_mask=signal,
    )
    assert len(hotspots) == 14, (
        "14 güçlü 300 m² adayın tamamı korunmalı; düşük aday tavanı saha "
        f"fırsatlarını kesiyor (çıktı: {len(hotspots)})."
    )


def main():
    check_configuration()
    check_coverage()
    check_scene_pair_coverage()
    check_small_site_path()
    check_candidate_capacity()
    print(
        "Uydu kalite kontrolü başarılı: tam zarf/tam-karo kapsamı, aynı göreli "
        "yörünge tercihi, 10 m ölçek, 250 m² küçük saha yolu ve yoğun-dönem "
        "aday kapasitesi korunuyor."
    )


if __name__ == "__main__":
    main()
