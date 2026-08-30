"""Post-dedupe şantiye kotası katmanını regresyon testiyle çalıştırır."""

import post_dedupe_construction_quota as quota


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
    raw.append(item(80, 1800, 0.9))
    updated, swaps = quota._swap_without_growth(current, raw, current)
    assert len(updated) == len(current)
    assert len(swaps) == 1
    assert sum(quota._is_construction(candidate) for candidate in updated) == 6
    assert sum(quota._is_wide(candidate) for candidate in updated) == 11

    duplicate_raw = list(current)
    duplicate_raw.append(
        {
            **item(81, 950, 1.0),
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
