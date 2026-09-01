"""Sentinel kör-alan denetimini daha geniş ama kontrollü devriye havuzuyla çalıştırır.

Üretim alarmına, Sentinel eşiklerine veya günlük saha görev sayısına dokunmaz.
``coverage_blind_area_audit`` ana diagnostik örneklerini 12 kayıtla sınırlı tutarken,
insan devriyesi için bilinen-kara körlük havuzunu daha geniş örnekler. Aynı yaklaşık
mahalle tek başına havuzu dolduramasın diye mahalle başına en fazla dört nokta,
bölge başına toplam en fazla 48 nokta tutulur. Günlük devriye yine ayrı katmanda
en fazla iki nokta seçer.
"""

from __future__ import annotations

from collections import defaultdict

import coverage_blind_area_audit as audit


PATROL_POOL_LIMIT = 48
PATROL_PER_NEIGHBORHOOD = 4


def _normalized_neighborhood(value):
    return " ".join(str(value or "Yakın bölge").strip().casefold().split())


def _patrol_sort_key(item):
    area = float(item.get("alan_m2") or 0)
    if area <= audit.PATROL_EARLY_MAX_AREA_M2:
        scale_rank = 0
    elif area <= audit.PATROL_TARGET_MAX_AREA_M2:
        scale_rank = 1
    else:
        scale_rank = 2
    reason_rank = 0 if item.get("neden") == "BULUT_GOLGE_KALICI" else 1
    area_rank = area if scale_rank < 2 else -area
    return (
        scale_rank,
        reason_rank,
        area_rank,
        str(item.get("mahalle_yaklasik") or ""),
        float(item.get("enlem") or 0),
        float(item.get("boylam") or 0),
    )


def _expanded_patrol_component_examples(components):
    """En iyi körlük örneklerini mahalle yığılması olmadan devriye havuzuna al."""
    ordered = sorted(
        [item for item in components if isinstance(item, dict)],
        key=_patrol_sort_key,
    )
    counts = defaultdict(int)
    selected = []
    for item in ordered:
        neighborhood = _normalized_neighborhood(item.get("mahalle_yaklasik"))
        if counts[neighborhood] >= PATROL_PER_NEIGHBORHOOD:
            continue
        selected.append(item)
        counts[neighborhood] += 1
        if len(selected) >= PATROL_POOL_LIMIT:
            break
    return selected


def _self_check():
    sample = []
    for neighborhood_index, neighborhood in enumerate(("Alaçatı", "Dalyan", "Ildır")):
        for index in range(8):
            sample.append(
                {
                    "mahalle_yaklasik": neighborhood,
                    "enlem": 38.20 + neighborhood_index * 0.05 + index * 0.0001,
                    "boylam": 26.30 + neighborhood_index * 0.05 + index * 0.0001,
                    "alan_m2": 300 + index * 100,
                    "neden": "BULUT_GOLGE_KALICI",
                }
            )
    picked = _expanded_patrol_component_examples(sample)
    assert len(picked) == 12, picked
    counts = defaultdict(int)
    for item in picked:
        counts[_normalized_neighborhood(item.get("mahalle_yaklasik"))] += 1
    assert set(counts.values()) == {PATROL_PER_NEIGHBORHOOD}, counts
    assert len(picked) <= PATROL_POOL_LIMIT


def main():
    _self_check()
    audit._patrol_component_examples = _expanded_patrol_component_examples
    audit.main()


if __name__ == "__main__":
    main()
