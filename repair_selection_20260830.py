"""30 Ağustos aynı-Sentinel seçim göçünü tek seferlik güvenli biçimde onarır.

Spektral güç sıralaması ölçüm olarak faydalı çıktı; fakat mevcut 29 Ağustos Sentinel
sahnesine geriye dönük uygulanınca yeni görevler açıp saha kuyruğunu geçici olarak
şişirdi. Bu göç betiği yalnız 2026-08-30 tarihinde ve `yeni_goruntu=0` olan aynı sahne
için son kararlı v3 seçim politikasını yeniden üretir. 250 m² eşiği, spektral filtreler,
24 aday tavanı, 6 şantiye-ölçeği kotası ve diyagonal yan-küme koruması değişmez.

Dosya veri onarımı tamamlanınca depodan kaldırılacaktır; kalıcı seçim motoru yeni
spektral sıralamayı yalnız ilk yeni Sentinel sahnesinde devreye alır.
"""

from __future__ import annotations

import json
from datetime import datetime

import satellite
import rebalance_satellite_candidates as rebalance
from daily_report import ISTANBUL, REPORT_REGIONS, build_daily_report, ensure_daily_schema
from scanner import connect


REPAIR_DATE = "2026-08-30"


def _legacy_balanced_select(
    candidates,
    limit=satellite.HOTSPOT_LIMIT,
    small_quota=satellite.SMALL_HOTSPOT_QUOTA,
    construction_quota=rebalance.CONSTRUCTION_SCALE_QUOTA,
    construction_early_quota=rebalance.CONSTRUCTION_EARLY_QUOTA,
    diagonal_sidecar_quota=rebalance.DIAGONAL_SIDECAR_QUOTA,
):
    """v3 alan-dengesi + diyagonal yan-küme politikasını birebir yeniden üret."""
    ranked = sorted(
        [item for item in candidates if isinstance(item, dict)],
        key=lambda item: (
            -rebalance._area(item),
            float(item.get("enlem") or 0),
            float(item.get("boylam") or 0),
        ),
    )
    limit = max(int(limit), 0)
    if len(ranked) <= limit:
        return ranked

    small = [
        item for item in ranked
        if rebalance._area(item) < rebalance.CONSTRUCTION_SCALE_MIN_M2
    ]
    construction = [
        item for item in ranked
        if rebalance.CONSTRUCTION_SCALE_MIN_M2
        <= rebalance._area(item)
        <= rebalance.CONSTRUCTION_SCALE_MAX_M2
    ]
    wide = [
        item for item in ranked
        if rebalance._area(item) > rebalance.CONSTRUCTION_SCALE_MAX_M2
    ]

    sidecars = [
        item for item in construction
        if item.get("geometri_kaynagi") == rebalance.DIAGONAL_SIDECAR_TAG
    ]
    regular_construction = [
        item for item in construction
        if item.get("geometri_kaynagi") != rebalance.DIAGONAL_SIDECAR_TAG
    ]

    selected = []
    selected.extend(small[: min(max(int(small_quota), 0), limit)])

    remaining = limit - len(selected)
    construction_slots = min(max(int(construction_quota), 0), remaining)
    sidecar_slots = min(
        max(int(diagonal_sidecar_quota), 0),
        construction_slots,
        len(sidecars),
    )
    sidecar_selected = sorted(
        sidecars,
        key=lambda item: (
            rebalance._area(item),
            float(item.get("enlem") or 0),
            float(item.get("boylam") or 0),
        ),
    )[:sidecar_slots]
    selected.extend(sidecar_selected)

    regular_slots = construction_slots - len(sidecar_selected)
    early_slots = min(max(int(construction_early_quota), 0), regular_slots)
    construction_asc = sorted(
        regular_construction,
        key=lambda item: (
            rebalance._area(item),
            float(item.get("enlem") or 0),
            float(item.get("boylam") or 0),
        ),
    )
    early_selected = construction_asc[:early_slots]
    selected.extend(early_selected)

    remaining_construction_slots = regular_slots - len(early_selected)
    if remaining_construction_slots > 0:
        early_keys = {
            key
            for key in map(rebalance._candidate_key, early_selected)
            if key is not None
        }
        upper_construction = [
            item for item in regular_construction
            if (key := rebalance._candidate_key(item)) is not None
            and key not in early_keys
        ]
        selected.extend(upper_construction[:remaining_construction_slots])

    remaining = limit - len(selected)
    selected.extend(wide[:remaining])

    if len(selected) < limit:
        selected_keys = {
            key for key in map(rebalance._candidate_key, selected) if key is not None
        }
        leftovers = [
            item for item in ranked
            if (key := rebalance._candidate_key(item)) is not None
            and key not in selected_keys
        ]
        selected.extend(leftovers[: limit - len(selected)])

    return sorted(
        selected[:limit], key=lambda item: rebalance._area(item), reverse=True
    )


def _self_check():
    def item(index, area, **extra):
        return {
            "enlem": 38.20 + index * 0.0001,
            "boylam": 26.30 + index * 0.0001,
            "alan_m2": area,
            **extra,
        }

    synthetic = []
    synthetic.extend(item(i, 300 + i * 50) for i in range(6))
    synthetic.extend(item(20 + i, 1000 + i * 500) for i in range(12))
    synthetic.extend(item(50 + i, 11000 + i * 1000) for i in range(30))
    synthetic.append(
        item(90, 1200, geometri_kaynagi=rebalance.DIAGONAL_SIDECAR_TAG)
    )
    selected = _legacy_balanced_select(synthetic)
    counts = rebalance._bucket_counts(selected)
    assert len(selected) == satellite.HOTSPOT_LIMIT
    assert counts == {"kucuk": 6, "santiye_olcegi": 6, "genis": 12}, counts
    assert sum(
        candidate.get("geometri_kaynagi") == rebalance.DIAGONAL_SIDECAR_TAG
        for candidate in selected
    ) == 1


def repair_selection():
    ensure_daily_schema()
    _self_check()
    report_date = datetime.now(ISTANBUL).strftime("%Y-%m-%d")
    if report_date != REPAIR_DATE:
        return []

    repaired = []
    with connect() as connection:
        for region_key in REPORT_REGIONS:
            row = connection.execute(
                """SELECT son_item,hata,yeni_goruntu FROM gunluk_uydu_raporlari
                WHERE rapor_tarihi=? AND bolge=? LIMIT 1""",
                (report_date, region_key),
            ).fetchone()
            if not row or row[1] or not row[0] or bool(row[2]):
                continue

            latest_item = str(row[0])
            pair = satellite.sentinel_pair(region_key)
            if pair[1].get("id") != latest_item:
                continue

            raw_result = rebalance._uncapped_analysis(region_key, pair)
            raw = [
                item for item in raw_result.get("hotspots", [])
                if isinstance(item, dict)
            ]
            selected = _legacy_balanced_select(raw)
            counts = rebalance._bucket_counts(selected)
            connection.execute(
                """UPDATE gunluk_uydu_raporlari SET hareket_json=?
                WHERE rapor_tarihi=? AND bolge=? AND son_item=?""",
                (
                    json.dumps(selected, ensure_ascii=False),
                    report_date,
                    region_key,
                    latest_item,
                ),
            )
            rebalance._reset_complementary_state(connection, region_key)
            rebalance._store_selection_state(
                connection,
                region_key,
                latest_item,
                len(raw),
                selected,
            )
            repaired.append((region_key, len(raw), len(selected), counts))

    if repaired:
        build_daily_report()
    return repaired


def main():
    repaired = repair_selection()
    if repaired:
        detail = " | ".join(
            f"{region}: ham {raw} → {selected}; 250-800={counts['kucuk']}, "
            f"800-10000={counts['santiye_olcegi']}, >10000={counts['genis']}"
            for region, raw, selected, counts in repaired
        )
        print("Tek seferlik aynı-sahne seçim onarımı uygulandı: " + detail)
    else:
        print("Tek seferlik aynı-sahne seçim onarımı için işlem gerekmedi.")


if __name__ == "__main__":
    main()
