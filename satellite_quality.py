"""Şantiye Radarı uydu motorunun kritik mekânsal varsayımlarını doğrular.

Bu test gerçek Sentinel servisine bağlanmaz. Ama günlük taramanın temel hedeflerini
korur: Çeşme + Uzunkuyu kapsamasında boşluk oluşmaması, analizin yaklaşık 10 m
piksel ölçeğinde kalması ve 250 m² sınıfındaki güçlü küçük saha sinyalinin
algoritma değişiklikleri sırasında yanlışlıkla kaybolmaması.
"""

from __future__ import annotations

import numpy as np

from satellite import (
    MAX_ANALYSIS_DIMENSION,
    MIN_HOTSPOT_AREA_M2,
    PLACE_CENTERS,
    REGIONS,
    SMALL_HOTSPOT_MIN_PIXELS,
    TARGET_PIXEL_SIZE_M,
    _clean_mask,
    _hotspots,
    _output_shape,
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

    # Sadece mahalle merkezlerini test etmek kör kıyı şeritlerini kaçırabilir.
    # Günlük iki tarama kutusunun, tanımlı tüm Çeşme + Uzunkuyu zarfını noktasal
    # bir ızgara üzerinde eksiksiz kapladığını da doğrula.
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

    # Çeşme kutusundan Uzunkuyu kutusuna doğu-batı yönünde kör şerit oluşmasın.
    cesme = REGIONS["cesme"]["bbox"]
    uzunkuyu = REGIONS["uzunkuyu"]["bbox"]
    assert cesme[2] >= uzunkuyu[0], (
        "Çeşme ve Uzunkuyu günlük tarama kutuları arasında boylam boşluğu var."
    )


def check_small_site_path():
    # Yaklaşık 10 m piksellerde üç bitişik güçlü piksel ≈ 300 m². Bu, kullanıcının
    # 250 m² minimum hedefinin pratikte yakalanabildiğini garanti eden regresyon testidir.
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

    # İki piksellik (~200 m²) gürültü ise minimum eşik altındadır ve görev üretmemeli.
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


def main():
    check_configuration()
    check_coverage()
    check_small_site_path()
    print("Uydu kalite kontrolü başarılı: tam zarf kapsaması, 10 m ölçek ve 250 m² küçük saha yolu korunuyor.")


if __name__ == "__main__":
    main()
