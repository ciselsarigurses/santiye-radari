"""Post-dedupe şantiye kotası katmanını regresyon testiyle çalıştırır.

Erken hafriyat hedefi için toplam alarm sayısını büyütmeden 800-10.000 m²
şantiye/parsel ölçeği tabanını 6'dan 8'e çıkarır. Ana Sentinel spektral eşikleri,
250 m² alt sınırı ve küçük-saha kotası değişmez. Ek iki yer yalnız en zayıf geniş
>10.000 m² adaylarla takas edilir ve politika mevcut Sentinel sahnesini geriye
dönük karıştırmadan ilk yeni sahnede devreye girer.
"""

import post_dedupe_construction_quota as quota


TARGET_CONSTRUCTION_QUOTA = 8
quota.rebalance.CONSTRUCTION_SCALE_QUOTA = TARGET_CONSTRUCTION_QUOTA
quota.POLICY_VERSION = "post-dedupe-construction-quota-v2-target8-next-scene"


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

    raw = list(current)
    raw.extend(
        [
            item(80, 1800, 0.90),
            item(81, 3200, 0.85),
            item(82, 4800, 0.80),
        ]
    )
    updated, swaps = quota._swap_without_growth(current, raw, current)
    assert len(updated) == len(current)
    assert len(swaps) == 3
    assert sum(quota._is_construction(candidate) for candidate in updated) == 8
    assert sum(quota._is_wide(candidate) for candidate in updated) == 9

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
