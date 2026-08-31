"""Post-dedupe şantiye kotası katmanını regresyon testiyle çalıştırır.

Erken hafriyat hedefi için toplam alarm sayısını büyütmeden 800-10.000 m²
şantiye/parsel ölçeği tabanını 6'dan 8'e çıkarır. Bu sekiz yerin içinde
800-2.000 m² erken-parsel tabanını da 3'ten 4'e yükseltir. Ana Sentinel spektral
eşikleri, 250 m² alt sınırı ve küçük-saha kotası değişmez. Ek yerler yalnız en
zayıf geniş >10.000 m² adaylarla takas edilir ve politika mevcut Sentinel sahnesini
geriye dönük karıştırmadan ilk yeni sahnede devreye girer.

8-komşu kümede geniş bir değişime yalnız köşeden bağlandığı için görünmez hale gelen
800-10.000 m² parsel parçası varsa, çekirdek rebalance katmanının zaten sıkı şekilde
ürettiği en iyi tek ``DIYAGONAL_YAN_KUME`` adayı post-dedupe kota onarımında normal
üst-parsel adaylarından önce değerlendirilir. Erken 800-2.000 m² düzenli adaylar yine
önceliklidir; toplam alarm sayısı ve en fazla bir diyagonal yan-küme sınırı değişmez.
"""

import post_dedupe_construction_quota as quota


TARGET_CONSTRUCTION_QUOTA = 8
TARGET_EARLY_QUOTA = 4
quota.rebalance.CONSTRUCTION_SCALE_QUOTA = TARGET_CONSTRUCTION_QUOTA
quota.rebalance.CONSTRUCTION_EARLY_QUOTA = TARGET_EARLY_QUOTA
quota.POLICY_VERSION = "post-dedupe-construction-quota-v4-sidecar-priority-next-scene"


def _choose_additions_with_sidecar_priority(raw, current, selected_all, missing):
    """Erken düzenli parselden sonra en iyi tek gizli diyagonal parseli değerlendir."""
    if missing <= 0:
        return []

    current_early = sum(quota._is_early_construction(item) for item in current)
    early_needed = min(
        max(quota.rebalance.CONSTRUCTION_EARLY_QUOTA - current_early, 0),
        missing,
    )
    additions = []

    regular = [
        item for item in raw
        if quota._is_construction(item)
        and item.get("geometri_kaynagi") != quota.rebalance.DIAGONAL_SIDECAR_TAG
    ]
    sidecars = [
        item for item in raw
        if quota._is_construction(item)
        and item.get("geometri_kaynagi") == quota.rebalance.DIAGONAL_SIDECAR_TAG
    ]
    current_has_sidecar = any(
        item.get("geometri_kaynagi") == quota.rebalance.DIAGONAL_SIDECAR_TAG
        for item in current
    )

    early_pool = [item for item in regular if quota._is_early_construction(item)]
    for candidate in quota._rank_pool(early_pool, early=True):
        if len(additions) >= early_needed:
            break
        if quota._candidate_available(candidate, selected_all, additions):
            additions.append(candidate)

    # Çekirdek rebalance zaten yalnız geniş 8-komşu ebeveynin küçük bir kısmı olan
    # 4-komşu yan kümeleri sidecar yapar. Kota onarımı eksikse ve mevcut seçimde
    # sidecar yoksa, bu güvenli havuzdan spektral olarak en güçlü tek adayı normal
    # üst-parsel havuzundan önce dene. Alarm sayısı büyümez.
    if len(additions) < missing and not current_has_sidecar:
        for candidate in quota._rank_pool(sidecars, early=False):
            if quota._candidate_available(candidate, selected_all, additions):
                additions.append(candidate)
                break

    if len(additions) < missing:
        upper_pool = [item for item in regular if not quota._is_early_construction(item)]
        for candidate in quota._rank_pool(upper_pool, early=False):
            if len(additions) >= missing:
                break
            if quota._candidate_available(candidate, selected_all, additions):
                additions.append(candidate)

    if len(additions) < missing:
        fallback = [item for item in regular if item not in additions]
        for candidate in quota._rank_pool(fallback, early=False):
            if len(additions) >= missing:
                break
            if quota._candidate_available(candidate, selected_all, additions):
                additions.append(candidate)

    return additions[:missing]


quota._choose_additions = _choose_additions_with_sidecar_priority


def _fixed_self_check():
    def item(index, area, strength=0.0, **extra):
        return {
            "enlem": 38.20 + index * 0.001,
            "boylam": 26.30 + index * 0.001,
            "alan_m2": area,
            quota.rebalance.STRONG_SIGNAL_FIELD: strength,
            **extra,
        }

    current = [item(1, 400, 0.9)]
    current.extend(item(10 + i, 1000 + i * 500, 0.5) for i in range(5))
    current.extend(item(30 + i, 20000 + i * 1000, 0.1) for i in range(12))

    sidecar = item(
        79,
        3200,
        0.95,
        geometri_kaynagi=quota.rebalance.DIAGONAL_SIDECAR_TAG,
    )
    raw = list(current)
    raw.extend(
        [
            item(80, 1800, 0.90),
            sidecar,
            item(82, 4800, 0.80),
        ]
    )
    updated, swaps = quota._swap_without_growth(current, raw, current)
    assert len(updated) == len(current)
    assert len(swaps) == 3
    assert sum(quota._is_construction(candidate) for candidate in updated) == 8
    assert sum(quota._is_early_construction(candidate) for candidate in updated) >= 4
    assert sum(quota._is_wide(candidate) for candidate in updated) == 9
    assert any(
        candidate.get("geometri_kaynagi") == quota.rebalance.DIAGONAL_SIDECAR_TAG
        for candidate in updated
    ), "Gizli diyagonal parsel kota onarımında temsil edilmedi."

    duplicate_raw = list(current)
    duplicate_raw.append(
        {
            **item(90, 950, 1.0),
            "enlem": current[1]["enlem"] + 0.00001,
            "boylam": current[1]["boylam"] + 0.00001,
        }
    )
    unchanged, duplicate_swaps = quota._swap_without_growth(
        current, duplicate_raw, current
    )
    assert len(unchanged) == len(current)
    assert not duplicate_swaps, duplicate_swaps


quota._self_check = _fixed_self_check
quota.main()
