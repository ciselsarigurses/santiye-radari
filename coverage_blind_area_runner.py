"""Sentinel kör-alan denetimini daha geniş ama kontrollü devriye havuzuyla çalıştırır.

Üretim alarmına, Sentinel eşiklerine veya günlük saha görev sayısına dokunmaz.
``coverage_blind_area_audit`` ana diagnostik örneklerini 12 kayıtla sınırlı tutarken,
insan devriyesi için bilinen-kara körlük havuzunu daha geniş örnekler. Aynı yaklaşık
mahalle tek başına havuzu dolduramasın diye mahalle başına en fazla dört nokta,
bölge başına toplam en fazla 48 nokta tutulur. Ayrıca aynı körlük cebindeki çok yakın
kümeler devriye havuzunu doldurmasın diye seçilen örnekler arasında en az 500 m
mekânsal ayrım aranır. Günlük devriye yine ayrı katmanda en fazla iki nokta seçer.
"""

from __future__ import annotations

from collections import defaultdict
import math

import coverage_blind_area_audit as audit


PATROL_POOL_LIMIT = 48
PATROL_PER_NEIGHBORHOOD = 4
PATROL_MIN_SPACING_M = 500


def _normalized_neighborhood(value):
    return " ".join(str(value or "Yakın bölge").strip().casefold().split())


def _point(item):
    try:
        return float(item.get("enlem")), float(item.get("boylam"))
    except (TypeError, ValueError, AttributeError):
        return None


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _far_enough(point, selected_points, minimum_m=PATROL_MIN_SPACING_M):
    return all(_distance_m(point, other) >= minimum_m for other in selected_points)


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
    """En iyi körlük örneklerini mahalle ve yakın-konum yığılması olmadan seç."""
    ordered = sorted(
        [item for item in components if isinstance(item, dict)],
        key=_patrol_sort_key,
    )
    counts = defaultdict(int)
    selected = []
    selected_points = []
    for item in ordered:
        neighborhood = _normalized_neighborhood(item.get("mahalle_yaklasik"))
        if counts[neighborhood] >= PATROL_PER_NEIGHBORHOOD:
            continue
        point = _point(item)
        if point is None:
            continue
        # Aynı kalıcı bulut/gölge cebindeki birkaç 10 m kümenin ardışık günlerde
        # fiilen aynı sokağı yeniden kontrol ettirmesini önle. Yeterli ayrı kör alan
        # varsa havuz yarımadaya yayılır; alarm/görev sayısı bundan etkilenmez.
        if not _far_enough(point, selected_points):
            continue
        selected.append(item)
        selected_points.append(point)
        counts[neighborhood] += 1
        if len(selected) >= PATROL_POOL_LIMIT:
            break
    return selected


def _self_check():
    # Aynı mahallede ve aynı küçük cepteki yoğun kümeler tek devriye örneğine inmeli.
    clustered = []
    for neighborhood_index, neighborhood in enumerate(("Alaçatı", "Dalyan", "Ildır")):
        for index in range(8):
            clustered.append(
                {
                    "mahalle_yaklasik": neighborhood,
                    "enlem": 38.20 + neighborhood_index * 0.05 + index * 0.00005,
                    "boylam": 26.30 + neighborhood_index * 0.05 + index * 0.00005,
                    "alan_m2": 300 + index * 100,
                    "neden": "BULUT_GOLGE_KALICI",
                }
            )
    clustered_picked = _expanded_patrol_component_examples(clustered)
    assert len(clustered_picked) == 3, clustered_picked

    # Aynı yaklaşık mahallede gerçekten ayrı sokak/ceplere düşen noktalar korunur,
    # fakat mahalle başına dört kayıt tavanı yine geçilmez.
    spread = []
    for index in range(7):
        spread.append(
            {
                "mahalle_yaklasik": "Alaçatı",
                "enlem": 38.20 + index * 0.01,
                "boylam": 26.30,
                "alan_m2": 300 + index * 100,
                "neden": "BULUT_GOLGE_KALICI",
            }
        )
    spread_picked = _expanded_patrol_component_examples(spread)
    assert len(spread_picked) == PATROL_PER_NEIGHBORHOOD, spread_picked

    mixed = clustered + spread
    picked = _expanded_patrol_component_examples(mixed)
    counts = defaultdict(int)
    points = []
    for item in picked:
        counts[_normalized_neighborhood(item.get("mahalle_yaklasik"))] += 1
        point = _point(item)
        assert point is not None
        assert _far_enough(point, points), (item, points)
        points.append(point)
    assert all(value <= PATROL_PER_NEIGHBORHOOD for value in counts.values()), counts
    assert len(picked) <= PATROL_POOL_LIMIT


def main():
    _self_check()
    audit._patrol_component_examples = _expanded_patrol_component_examples
    audit.main()


if __name__ == "__main__":
    main()
