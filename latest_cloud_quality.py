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
    # SCL: 4=vegetation (geçerli), 6=water, 8=cloud medium probability.
    primary = np.full((4, 5), 4, dtype="uint8")
    latest = np.full((4, 5), 4, dtype="uint8")
    fallback = np.full((4, 5), 4, dtype="uint8")

    # Yalnız en yeni görüntü bulutlu; önceki iki sahne açık -> tamamlanmalı.
    latest[1, 2] = 8
    zone = latest_gap_zone(primary, latest, fallback)
    assert bool(zone[1, 2]), (
        "En yeni sahnede bulut altında kalan, önceki iki sahnede açık piksel "
        "zaman-serisi son-açık-kanıt yoluna girmiyor."
    )

    # Su/no-data gibi kalıcı/jeometrik sınıflar bulut gibi geçmişten doldurulmasın.
    latest[2, 1] = 6
    zone = latest_gap_zone(primary, latest, fallback)
    assert not bool(zone[2, 1]), (
        "Su sınıfı en-yeni bulut boşluğu gibi doldurulmamalı; kıyı yanlış pozitifi artar."
    )

    # En yeni bulutlu olsa bile son açık diye kullanılacak primary de geçersizse
    # değişim kanıtı yoktur; aday üretilmemeli.
    latest[3, 3] = 9
    primary[3, 3] = 3
    zone = latest_gap_zone(primary, latest, fallback)
    assert not bool(zone[3, 3]), (
        "Primary sahne de geçersizken son-açık-kanıt varmış gibi davranılıyor."
    )

    # Yedek sahne geçersizse karşılaştırılabilir başlangıç kanıtı yoktur.
    latest[0, 4] = 10
    fallback[0, 4] = 11
    zone = latest_gap_zone(primary, latest, fallback)
    assert not bool(zone[0, 4]), (
        "Yedek sahne geçersizken en-yeni bulut boşluğu karşılaştırması yapılmamalı."
    )


def check_class_policy():
    transient = set(int(value) for value in TRANSIENT_LATEST_CLASSES.tolist())
    excluded = set(int(value) for value in EXCLUDED_CLASSES.tolist())
    assert transient == {3, 8, 9, 10, 11}, (
        "En-yeni körlük yalnız geçici bulut/gölge/kar sınıflarında çalışmalı."
    )
    assert 0 not in transient and 6 not in transient
    assert transient.issubset(excluded)
    assert LATEST_CLOUD_GAP_VERSION.startswith("latest-cloud-gap-v1")


def main():
    check_latest_gap_zone()
    check_class_policy()
    print(
        "En yeni görüntü bulut-körlük kalite kontrolü başarılı: yalnız geçici "
        "bulut/gölge piksellerinde, önceki ve yedek sahne açıkken son-açık-kanıt "
        "tamamlaması etkin; su/no-data korunuyor."
    )


if __name__ == "__main__":
    main()
