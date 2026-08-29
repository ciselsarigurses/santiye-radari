"""En yeni Sentinel sahnesi bulut/gölge körlüğünün ağsız kalite kontrolü."""

from __future__ import annotations

import numpy as np

from latest_cloud_gap_scan import (
    LATEST_CLOUD_GAP_VERSION,
    TRANSIENT_LATEST_CLASSES,
    latest_gap_zone,
)
from temporal_gap_scan import EXCLUDED_CLASSES


def check_latest_gap_zone():
    # SCL: 4=vegetation (geçerli), 2=cast shadow, 6=water, 8=cloud medium probability.
    primary = np.full((4, 5), 4, dtype="uint8")
    latest = np.full((4, 5), 4, dtype="uint8")
    fallback = np.full((4, 5), 4, dtype="uint8")

    latest[1, 2] = 8
    zone = latest_gap_zone(primary, latest, fallback)
    assert bool(zone[1, 2]), (
        "En yeni sahnede bulut altında kalan, önceki iki sahnede açık piksel zaman-serisi son-açık-kanıt yoluna girmiyor."
    )

    # PB04.00+ SCL2 cast shadow da geçici körlüktür; iki açık sahne varsa geri kazanılmalı.
    latest[1, 3] = 2
    zone = latest_gap_zone(primary, latest, fallback)
    assert bool(zone[1, 3]), (
        "En yeni sahnedeki SCL2 cast shadow açık sahne kanıtıyla geri kazanılmıyor."
    )

    latest[2, 1] = 6
    zone = latest_gap_zone(primary, latest, fallback)
    assert not bool(zone[2, 1]), (
        "Su sınıfı en-yeni bulut boşluğu gibi doldurulmamalı; kıyı yanlış pozitifi artar."
    )

    latest[3, 3] = 9
    primary[3, 3] = 3
    zone = latest_gap_zone(primary, latest, fallback)
    assert not bool(zone[3, 3]), (
        "Primary sahne de geçersizken son-açık-kanıt varmış gibi davranılıyor."
    )

    latest[0, 4] = 10
    fallback[0, 4] = 11
    zone = latest_gap_zone(primary, latest, fallback)
    assert not bool(zone[0, 4]), (
        "Yedek sahne geçersizken en-yeni bulut boşluğu karşılaştırması yapılmamalı."
    )


def check_class_policy():
    transient = set(int(value) for value in TRANSIENT_LATEST_CLASSES.tolist())
    excluded = set(int(value) for value in EXCLUDED_CLASSES.tolist())
    assert transient == {2, 3, 8, 9, 10, 11}, (
        "En-yeni körlük SCL2 cast shadow dahil yalnız geçici gölge/bulut/kar sınıflarında çalışmalı."
    )
    assert 0 not in transient and 6 not in transient
    assert 7 not in excluded, "Gerçek koyu toprak/dark feature SCL7 doğrudan analizde geçerli kalmalı."
    assert transient.issubset(excluded)
    assert LATEST_CLOUD_GAP_VERSION.startswith("latest-cloud-gap-v2")


def main():
    check_latest_gap_zone()
    check_class_policy()
    print(
        "En yeni görüntü bulut-körlük kalite kontrolü başarılı: SCL2 cast shadow dahil geçici gölge/bulut pikselleri yalnız iki açık sahne kanıtıyla geri kazanılıyor; su/no-data korunuyor."
    )


if __name__ == "__main__":
    main()
