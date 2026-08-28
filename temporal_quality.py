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


def _item(item_id, date, tile="35SNC"):
    return {
        "id": item_id,
        "properties": {
            "datetime": f"{date}T08:30:00Z",
            "s2:mgrs_tile": tile,
        },
    }


def check_fallback_selection():
    latest = _item("latest", "2026-08-26")
    primary = _item("primary", "2026-08-24")
    too_recent = _item("recent", "2026-08-21")
    wrong_tile = _item("wrong-tile", "2026-08-18", tile="35SND")
    fallback = _item("fallback", "2026-08-19")
    selected = select_fallback(
        [latest, primary, too_recent, wrong_tile, fallback],
        latest,
        primary,
    )
    assert FALLBACK_MIN_GAP_DAYS == 7
    assert selected and selected["id"] == "fallback", (
        "Zaman-serisi yedeği en az 7 gün eski ve aynı Sentinel karosunda seçilmeli."
    )


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
        "Zaman serisi kalite kontrolü başarılı: 7+ günlük aynı-karo yedeği, "
        "yalnız geçici bulut/gölge boşluğu ve 80 m mükerrer koruması doğrulandı."
    )


if __name__ == "__main__":
    main()
