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

250-800 m² küçük-saha kotası dolu olduğunda da alan büyüklüğü tek başına belirleyici
olmaz. Ana motorun zaten hesapladığı sert küçük-saha piksel oranı belirgin biçimde
daha yüksek bir ham aday varsa, seçili en zayıf küçük adayla bire bir takas edilir.
Küçük aday sayısı, toplam alarm sayısı ve eşikler değişmez; yalnız aynı alarm bütçesi
içinde hafriyat/temel sinyali daha güçlü olan nokta korunur. En az 0,05 güç farkı
aranarak küçük sayısal oynamalarda gereksiz seçim değişimi önlenir.
"""

import post_dedupe_construction_quota as quota


TARGET_CONSTRUCTION_QUOTA = 8
TARGET_EARLY_QUOTA = 4
MIN_SMALL_STRENGTH_GAIN = 0.05
quota.rebalance.CONSTRUCTION_SCALE_QUOTA = TARGET_CONSTRUCTION_QUOTA
quota.rebalance.CONSTRUCTION_EARLY_QUOTA = TARGET_EARLY_QUOTA
quota.POLICY_VERSION = "post-dedupe-construction-quota-v5-small-strength-next-scene"


_ORIGINAL_SWAP_WITHOUT_GROWTH = quota._swap_without_growth


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


def _is_small(item):
    area = quota._area(item)
    return (
        quota.satellite.MIN_HOTSPOT_AREA_M2
        <= area
        < quota.rebalance.CONSTRUCTION_SCALE_MIN_M2
    )


def _small_strength(item):
    return quota.rebalance._signal_strength(item)


def _improve_small_selection(current, raw, selected_all):
    """Küçük-saha sayısını artırmadan belirgin daha güçlü ham adayı seçime al."""
    updated = list(current)
    selected_small = [item for item in updated if _is_small(item)]
    quota_limit = min(
        max(int(quota.satellite.SMALL_HOTSPOT_QUOTA), 0),
        len(updated),
    )
    if quota_limit <= 0 or len(selected_small) < quota_limit:
        return updated, []

    current_keys = {
        key for key in map(quota.rebalance._candidate_key, updated) if key is not None
    }
    raw_small = [
        item for item in raw
        if _is_small(item)
        and quota.rebalance._candidate_key(item) not in current_keys
    ]
    if not raw_small:
        return updated, []

    weakest_selected = sorted(
        selected_small,
        key=lambda item: (
            _small_strength(item),
            -quota._area(item),
            float(item.get("enlem") or 0),
            float(item.get("boylam") or 0),
        ),
    )
    strongest_raw = sorted(
        raw_small,
        key=lambda item: (
            -_small_strength(item),
            -quota._area(item),
            float(item.get("enlem") or 0),
            float(item.get("boylam") or 0),
        ),
    )

    swaps = []
    for candidate in strongest_raw:
        if not weakest_selected:
            break
        removed = weakest_selected[0]
        if _small_strength(candidate) < _small_strength(removed) + MIN_SMALL_STRENGTH_GAIN:
            break

        comparison_selected = [
            item for item in selected_all
            if quota.rebalance._candidate_key(item)
            != quota.rebalance._candidate_key(removed)
        ]
        comparison_selected.extend(
            item for item in updated
            if quota.rebalance._candidate_key(item)
            != quota.rebalance._candidate_key(removed)
        )
        if not quota._candidate_available(candidate, comparison_selected, []):
            continue

        removed_key = quota.rebalance._candidate_key(removed)
        updated = [
            item for item in updated
            if quota.rebalance._candidate_key(item) != removed_key
        ]
        updated.append(candidate)
        swaps.append((removed, candidate))
        weakest_selected = [item for item in weakest_selected[1:] if item is not removed]
        weakest_selected.append(candidate)
        weakest_selected.sort(
            key=lambda item: (
                _small_strength(item),
                -quota._area(item),
                float(item.get("enlem") or 0),
                float(item.get("boylam") or 0),
            )
        )
        if len(swaps) >= quota_limit:
            break

    updated = sorted(updated, key=lambda item: quota._area(item), reverse=True)
    assert len(updated) == len(current), "Küçük-saha güç takası aday sayısını değiştirdi."
    assert sum(_is_small(item) for item in updated) == sum(
        _is_small(item) for item in current
    ), "Küçük-saha güç takası küçük aday sayısını değiştirdi."
    return updated, swaps


def _swap_with_small_strength_priority(current, raw, selected_all):
    updated, construction_swaps = _ORIGINAL_SWAP_WITHOUT_GROWTH(
        current, raw, selected_all
    )
    small_updated, small_swaps = _improve_small_selection(
        updated, raw, selected_all
    )
    return small_updated, construction_swaps + small_swaps


quota._swap_without_growth = _swap_with_small_strength_priority


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

    crowded_small = [
        item(100 + i, 300 + i * 70, 0.50 + i * 0.03)
        for i in range(6)
    ]
    crowded_small.extend(item(120 + i, 1000 + i * 400, 0.7) for i in range(8))
    crowded_small.extend(item(140 + i, 20000 + i * 1000, 0.1) for i in range(10))
    strong_small = item(170, 320, 0.95)
    crowded_raw = list(crowded_small) + [strong_small]
    small_updated, small_swaps = quota._swap_without_growth(
        crowded_small, crowded_raw, crowded_small
    )
    assert len(small_updated) == len(crowded_small)
    assert len(small_swaps) == 1, small_swaps
    assert sum(_is_small(candidate) for candidate in small_updated) == 6
    assert quota.rebalance._candidate_key(strong_small) in {
        quota.rebalance._candidate_key(candidate) for candidate in small_updated
    }, "Güçlü 250-800 m² küçük-saha adayı dolu kotada korunmadı."
    assert min(
        _small_strength(candidate) for candidate in small_updated if _is_small(candidate)
    ) > 0.50, "En zayıf küçük aday, belirgin güçlü aday varken seçimde kaldı."


quota._self_check = _fixed_self_check


def main():
    quota._self_check()
    changed, baselined, skipped = quota.repair_post_dedupe_quota()
    if changed:
        for region_key, swaps in changed:
            print(
                f"Post-dedupe seçim {region_key}: {len(swaps)} aday bire bir daha güçlü/"
                "erken saha adayıyla değiştirildi; toplam alarm sayısı değişmedi."
            )
    if baselined:
        for region_key, preview_count in baselined:
            print(
                f"Post-dedupe seçim tabanı {region_key}: mevcut sahne korunuyor; "
                f"ilk yeni Sentinel sahnesinde potansiyel takas={preview_count}."
            )
    if not changed and not baselined:
        print("Post-dedupe seçim politikası güncel; alarm sayısını değiştirecek işlem yok.")
    if skipped:
        print("Post-dedupe seçim atlanan/güncel bölgeler: " + ", ".join(skipped))


if __name__ == "__main__":
    main()
