"""Bulut/gölge zaman-serisi katmanının ağsız regresyon kontrolleri."""

from __future__ import annotations

from temporal_gap_scan import (
    DUPLICATE_METERS,
    EXCLUDED_CLASSES,
    FALLBACK_MIN_GAP_DAYS,
    TRANSIENT_OLDER_CLASSES,
    merge_candidates,
    select_fallback,
)


TEST_BBOX = [26.25, 38.22, 26.47, 38.43]
FULL_COVER = [26.20, 38.15, 26.52, 38.48]
PARTIAL_COVER = [26.35, 38.25, 26.52, 38.48]


def _item(item_id, date, tile="35SNC", orbit=7, footprint=None):
    return {
        "id": item_id,
        "bbox": footprint or FULL_COVER,
        "properties": {
            "datetime": f"{date}T08:30:00Z",
            "s2:mgrs_tile": tile,
            "sat:relative_orbit": orbit,
        },
    }


def check_fallback_selection():
    latest = _item("latest", "2026-08-26")
    primary = _item("primary", "2026-08-24")
    too_recent = _item("recent", "2026-08-21")
    wrong_tile = _item("wrong-tile", "2026-08-18", tile="35SND")
    partial = _item(
        "partial-newer",
        "2026-08-19",
        footprint=PARTIAL_COVER,
    )
    wrong_orbit = _item("wrong-orbit", "2026-08-18", orbit=36)
    fallback = _item("fallback", "2026-08-17", orbit=7)
    selected = select_fallback(
        [latest, primary, too_recent, wrong_tile, partial, wrong_orbit, fallback],
        latest,
        primary,
        bbox=TEST_BBOX,
    )
    assert FALLBACK_MIN_GAP_DAYS == 7
    assert selected and selected["id"] == "fallback", (
        "Zaman-serisi yedeği 7+ günlük, tam-kapsam aynı karoda olmalı ve mevcutsa "
        "aynı göreli yörünge farklı-yörünge sahnesine tercih edilmeli."
    )

    # Aynı-yörünge yedeği hiç yoksa tamamlayıcı katmanı tamamen kör bırakma;
    # tam-kapsam aynı-karo farklı-yörünge sahnesine güvenli geri dönüş korunur.
    selected_without_same_orbit = select_fallback(
        [latest, primary, wrong_orbit],
        latest,
        primary,
        bbox=TEST_BBOX,
    )
    assert selected_without_same_orbit and selected_without_same_orbit["id"] == "wrong-orbit"


def check_transient_classes():
    transient = set(int(value) for value in TRANSIENT_OLDER_CLASSES.tolist())
    excluded = set(int(value) for value in EXCLUDED_CLASSES.tolist())
    assert {3, 8, 9, 10, 11}.issubset(transient)
    assert 0 not in transient and 6 not in transient, (
        "No-data veya su sınıfı daha eski görüntüyle doldurulmamalı; kıyı/granül "
        "yanlış pozitif riski artar."
    )
    assert transient.issubset(excluded)


def check_deduplication():
    existing = [
        {
            "enlem": 38.300000,
            "boylam": 26.300000,
            "alan_m2": 1200,
            "boyut_sinifi": "STANDART",
        }
    ]
    recovered = [
        {
            # Yaklaşık 22 m: aynı saha ikinci görev olmamalı.
            "enlem": 38.300200,
            "boylam": 26.300000,
            "alan_m2": 500,
            "boyut_sinifi": "KUCUK",
        },
        {
            # Yeterince uzakta: bağımsız aday korunmalı.
            "enlem": 38.302000,
            "boylam": 26.300000,
            "alan_m2": 400,
            "boyut_sinifi": "KUCUK",
        },
    ]
    merged, additions = merge_candidates(existing, recovered)
    assert DUPLICATE_METERS == 80
    assert len(merged) == 2 and len(additions) == 1, (
        "Zaman-serisi katmanı yakın mükerreri ikinci saha görevi yapmamalı."
    )


def main():
    check_fallback_selection()
    check_transient_classes()
    check_deduplication()
    print(
        "Zaman serisi kalite kontrolü başarılı: 7+ günlük tam-kapsam aynı-karo "
        "yedeği, aynı göreli yörünge tercihi, yalnız geçici bulut/gölge boşluğu ve "
        "80 m mükerrer koruması doğrulandı."
    )


if __name__ == "__main__":
    main()
