"""Çakışma temizliği şantiye ölçeği kotasını azaltırsa alarm sayısını artırmadan düzeltir.

Çeşme ve Uzunkuyu analiz kutuları bilinçli olarak örtüşür. İki kutuda seçilen aynı
Sentinel kümesi daha sonra ``dedupe_overlap_satellite.py`` ile tek kayda indirilir.
Bu doğru davranış, fakat elenen kayıt 800-10.000 m² şantiye ölçeği kotasındaysa bir
bölge 6 yerine 5 şantiye ölçeği adayıyla kalabilir; buna karşılık çok geniş değişim
adayları listede kalır.

Bu katman yeni alarm üretmez ve eşikleri gevşetmez. Yalnız yeni Sentinel sahnesinde,
çakışma temizliği sonrasında 6 kişilik şantiye ölçeği kotası eksilmişse aynı bölgedeki
en zayıf/geniş >10.000 m² kaydı çıkarıp, mevcut 250 m²+ filtreleri zaten geçmiş ve
başka seçili adayla 25 m içinde mükerrer olmayan en iyi 800-10.000 m² adayı koyar.
Bölgenin ve toplam raporun aday sayısı değişmez. 250-800 m² güçlü küçük-saha adayları
hiçbir zaman bu işlemle çıkarılmaz.

Kod politikası değişiklikleri mevcut Sentinel sahnesini geriye dönük karıştırmaz.
İlk çalışmada mevcut sahne yalnız taban çizgisi olarak kaydedilir; düzeltme ilk yeni
Sentinel sahnesinde devreye girer.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import satellite
import rebalance_satellite_candidates as rebalance
from daily_report import ISTANBUL, REPORT_REGIONS, build_daily_report, ensure_daily_schema
from scanner import connect


POLICY_VERSION = "post-dedupe-construction-quota-v1-next-scene"
STATE_TABLE = "uydu_post_dedupe_kota_surumu"
DUPLICATE_METERS = 25.0
MIN_AREA_SIMILARITY = 0.70


def _area(item):
    return rebalance._area(item)


def _strength(item):
    return rebalance._signal_strength(item)


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _point(item):
    try:
        return float(item.get("enlem")), float(item.get("boylam"))
    except (TypeError, ValueError, AttributeError):
        return None


def _area_similarity(first, second):
    first_area = _area(first)
    second_area = _area(second)
    larger = max(first_area, second_area)
    if larger <= 0:
        return 0.0
    return min(first_area, second_area) / larger


def _same_size_class(first, second):
    first_area = _area(first)
    second_area = _area(second)
    first_small = first_area < rebalance.CONSTRUCTION_SCALE_MIN_M2
    second_small = second_area < rebalance.CONSTRUCTION_SCALE_MIN_M2
    return first_small == second_small


def _looks_same_site(first, second):
    first_point = _point(first)
    second_point = _point(second)
    if first_point is None or second_point is None:
        return False
    if not _same_size_class(first, second):
        return False
    if _area_similarity(first, second) < MIN_AREA_SIMILARITY:
        return False
    return _distance_m(first_point, second_point) <= DUPLICATE_METERS


def _is_construction(item):
    return (
        rebalance.CONSTRUCTION_SCALE_MIN_M2
        <= _area(item)
        <= rebalance.CONSTRUCTION_SCALE_MAX_M2
    )


def _is_early_construction(item):
    return _is_construction(item) and _area(item) <= rebalance.EARLY_PARCEL_MAX_M2


def _is_wide(item):
    return _area(item) > rebalance.CONSTRUCTION_SCALE_MAX_M2


def _candidate_available(candidate, selected_all, additions):
    key = rebalance._candidate_key(candidate)
    if key is None:
        return False
    for existing in [*selected_all, *additions]:
        if rebalance._candidate_key(existing) == key:
            return False
        if _looks_same_site(candidate, existing):
            return False
    return True


def _rank_pool(pool, early):
    if early:
        return sorted(
            pool,
            key=lambda item: (
                -_strength(item),
                _area(item),
                float(item.get("enlem") or 0),
                float(item.get("boylam") or 0),
            ),
        )
    return sorted(
        pool,
        key=lambda item: (
            -_strength(item),
            -_area(item),
            float(item.get("enlem") or 0),
            float(item.get("boylam") or 0),
        ),
    )


def _choose_additions(raw, current, selected_all, missing):
    if missing <= 0:
        return []

    current_early = sum(_is_early_construction(item) for item in current)
    early_needed = min(
        max(rebalance.CONSTRUCTION_EARLY_QUOTA - current_early, 0),
        missing,
    )
    additions = []

    regular = [
        item for item in raw
        if _is_construction(item)
        and item.get("geometri_kaynagi") != rebalance.DIAGONAL_SIDECAR_TAG
    ]
    sidecars = [
        item for item in raw
        if _is_construction(item)
        and item.get("geometri_kaynagi") == rebalance.DIAGONAL_SIDECAR_TAG
    ]
    current_has_sidecar = any(
        item.get("geometri_kaynagi") == rebalance.DIAGONAL_SIDECAR_TAG
        for item in current
    )

    early_pool = [item for item in regular if _is_early_construction(item)]
    for candidate in _rank_pool(early_pool, early=True):
        if len(additions) >= early_needed:
            break
        if _candidate_available(candidate, selected_all, additions):
            additions.append(candidate)

    remaining = missing - len(additions)
    if remaining <= 0:
        return additions

    upper_pool = [item for item in regular if not _is_early_construction(item)]
    for candidate in _rank_pool(upper_pool, early=False):
        if len(additions) >= missing:
            break
        if _candidate_available(candidate, selected_all, additions):
            additions.append(candidate)

    if len(additions) < missing and not current_has_sidecar:
        for candidate in _rank_pool(sidecars, early=False):
            if len(additions) >= missing:
                break
            if _candidate_available(candidate, selected_all, additions):
                additions.append(candidate)
                break

    if len(additions) < missing:
        fallback = [item for item in regular if item not in additions]
        for candidate in _rank_pool(fallback, early=False):
            if len(additions) >= missing:
                break
            if _candidate_available(candidate, selected_all, additions):
                additions.append(candidate)

    return additions[:missing]


def _wide_removal_order(current):
    """En zayıf spektral kanıtlı, eşitse en geniş yüzey değişimini önce çıkar."""
    return sorted(
        [item for item in current if _is_wide(item)],
        key=lambda item: (
            _strength(item),
            -_area(item),
            float(item.get("enlem") or 0),
            float(item.get("boylam") or 0),
        ),
    )


def _swap_without_growth(current, raw, selected_all):
    construction_count = sum(_is_construction(item) for item in current)
    missing = max(rebalance.CONSTRUCTION_SCALE_QUOTA - construction_count, 0)
    if missing <= 0:
        return list(current), []

    removals = _wide_removal_order(current)
    additions = _choose_additions(
        raw,
        current,
        selected_all,
        min(missing, len(removals)),
    )
    swap_count = min(len(removals), len(additions))
    if swap_count <= 0:
        return list(current), []

    remove_keys = {
        rebalance._candidate_key(item) for item in removals[:swap_count]
    }
    updated = [
        item for item in current
        if rebalance._candidate_key(item) not in remove_keys
    ]
    updated.extend(additions[:swap_count])
    updated = sorted(updated, key=lambda item: _area(item), reverse=True)

    assert len(updated) == len(current), "Post-dedupe takası aday sayısını değiştirdi."
    assert sum(_is_construction(item) for item in updated) == construction_count + swap_count
    assert sum(_area(item) < rebalance.CONSTRUCTION_SCALE_MIN_M2 for item in updated) == sum(
        _area(item) < rebalance.CONSTRUCTION_SCALE_MIN_M2 for item in current
    )
    return updated, list(zip(removals[:swap_count], additions[:swap_count]))


def _ensure_state_table(connection):
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
        bolge TEXT PRIMARY KEY,
        son_item TEXT NOT NULL,
        surum TEXT NOT NULL,
        guncelleme TEXT NOT NULL)"""
    )


def _load_movements(connection, report_date):
    rows = connection.execute(
        """SELECT bolge,son_item,hareket_json,hata FROM gunluk_uydu_raporlari
        WHERE rapor_tarihi=? ORDER BY bolge""",
        (report_date,),
    ).fetchall()
    values = {}
    for region_key, latest_item, movement_json, error in rows:
        if error or not latest_item:
            continue
        try:
            movement = json.loads(movement_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            movement = []
        if not isinstance(movement, list):
            movement = []
        values[str(region_key)] = {
            "latest_item": str(latest_item),
            "movement": [item for item in movement if isinstance(item, dict)],
        }
    return values


def _self_check():
    def item(index, area, strength=0.0, **extra):
        return {
            "enlem": 38.20 + index * 0.001,
            "boylam": 26.30 + index * 0.001,
            "alan_m2": area,
            rebalance.STRONG_SIGNAL_FIELD: strength,
            **extra,
        }

    current = [item(1, 400, 0.9)]
    current.extend(item(10 + i, 1000 + i * 500, 0.5) for i in range(5))
    current.extend(item(30 + i, 20000 + i * 1000, 0.1) for i in range(12))
    raw = list(current)
    raw.append(item(80, 1800, 0.9))
    updated, swaps = _swap_without_growth(current, raw, current)
    assert len(updated) == len(current)
    assert len(swaps) == 1
    assert sum(_is_construction(candidate) for candidate in updated) == 6
    assert sum(_is_wide(candidate) for candidate in updated) == 11

    duplicate_raw = list(current)
    duplicate_raw.append(
        {
            **item(81, 1800, 1.0),
            "enlem": current[1]["enlem"] + 0.00001,
            "boylam": current[1]["boylam"] + 0.00001,
        }
    )
    unchanged, duplicate_swaps = _swap_without_growth(current, duplicate_raw, current)
    assert len(unchanged) == len(current)
    assert not duplicate_swaps, duplicate_swaps


def repair_post_dedupe_quota():
    ensure_daily_schema()
    _self_check()
    report_date = datetime.now(ISTANBUL).strftime("%Y-%m-%d")
    changed = []
    baselined = []
    skipped = []

    with connect() as connection:
        _ensure_state_table(connection)
        movements = _load_movements(connection, report_date)
        selected_all = [
            item
            for data in movements.values()
            for item in data["movement"]
        ]

        for region_key in REPORT_REGIONS:
            data = movements.get(region_key)
            if not data:
                skipped.append(region_key)
                continue
            latest_item = data["latest_item"]
            state = connection.execute(
                f"SELECT son_item,surum FROM {STATE_TABLE} WHERE bolge=? LIMIT 1",
                (region_key,),
            ).fetchone()

            pair = satellite.sentinel_pair(region_key)
            if pair[1].get("id") != latest_item:
                skipped.append(region_key)
                continue
            raw_result = rebalance._uncapped_analysis(region_key, pair)
            raw = [
                item for item in raw_result.get("hotspots", [])
                if isinstance(item, dict)
            ]

            preview, preview_swaps = _swap_without_growth(
                data["movement"], raw, selected_all
            )

            if not state:
                connection.execute(
                    f"INSERT INTO {STATE_TABLE} (bolge,son_item,surum,guncelleme) VALUES(?,?,?,?)",
                    (
                        region_key,
                        latest_item,
                        POLICY_VERSION,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                baselined.append((region_key, len(preview_swaps)))
                continue

            if str(state[0]) == latest_item:
                if str(state[1]) != POLICY_VERSION:
                    connection.execute(
                        f"UPDATE {STATE_TABLE} SET surum=?,guncelleme=? WHERE bolge=?",
                        (
                            POLICY_VERSION,
                            datetime.now(timezone.utc).isoformat(),
                            region_key,
                        ),
                    )
                skipped.append(region_key)
                continue

            if preview_swaps:
                connection.execute(
                    """UPDATE gunluk_uydu_raporlari SET hareket_json=?
                    WHERE rapor_tarihi=? AND bolge=? AND son_item=?""",
                    (
                        json.dumps(preview, ensure_ascii=False),
                        report_date,
                        region_key,
                        latest_item,
                    ),
                )
                data["movement"] = preview
                selected_all = [
                    item
                    for key, region_data in movements.items()
                    for item in region_data["movement"]
                ]
                changed.append((region_key, preview_swaps))

            connection.execute(
                f"UPDATE {STATE_TABLE} SET son_item=?,surum=?,guncelleme=? WHERE bolge=?",
                (
                    latest_item,
                    POLICY_VERSION,
                    datetime.now(timezone.utc).isoformat(),
                    region_key,
                ),
            )

    if changed:
        build_daily_report()
    return changed, baselined, skipped


def main():
    _self_check()
    changed, baselined, skipped = repair_post_dedupe_quota()
    if changed:
        for region_key, swaps in changed:
            print(
                f"Post-dedupe şantiye kotası {region_key}: {len(swaps)} geniş aday, "
                f"aynı sayıda 800-10000 m² adayla değiştirildi; toplam alarm değişmedi."
            )
    if baselined:
        for region_key, preview_count in baselined:
            print(
                f"Post-dedupe kota tabanı {region_key}: mevcut sahne korunuyor; "
                f"ilk yeni Sentinel sahnesinde potansiyel takas={preview_count}."
            )
    if not changed and not baselined:
        print("Post-dedupe şantiye kotası güncel; alarm sayısını değiştirecek işlem yok.")
    if skipped:
        print("Post-dedupe kota atlanan/güncel bölgeler: " + ", ".join(skipped))


if __name__ == "__main__":
    main()
