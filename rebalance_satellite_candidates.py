"""Sentinel 24-aday tavanında şantiye ölçeğindeki kümeler için kontrollü temsil sağlar.

Ana uydu motorunun 250 m², yaklaşık 10 m ve spektral eşiklerini değiştirmez. Yalnız
ana motorun aynı görüntüde ürettiği tüm geçerli kümeler arasından 24 kayıt seçilirken
250-800 m² güçlü küçük saha adaylarını korur, 800-10.000 m² aralığına sınırlı bir kota
ayırır ve kalan kapasiteyi geniş değişimlere bırakır. Şantiye ölçeği kotasının bir
bölümü bandın küçük ucundan seçilerek erken hafriyat adaylarının 9-10 bin m²'lik
kümeler tarafından tamamen gömülmesi önlenir; kotanın kalan kısmı büyük uçtan seçilir.

8-komşuluk nedeniyle 10.000 m² üzerindeki geniş bir kümeye yalnız köşeden bağlanan
800-10.000 m²'lik küçük bir yan küme varsa ve geniş kümenin 4-komşu ana gövdesi hâlâ
10.000 m² üzerindeyse bu yan parça ayrıca ölçülür. Yan parçanın ebeveynin en fazla
%35'i olması gerekir; böylece tek bir gerçek hafriyatın iki benzer yarıya bölünmesi
engellenir. Toplam 24 aday ve 6 şantiye-ölçeği kotası artmaz; bu tür gömülü parseller
için en fazla 1 şantiye-ölçeği yeri ayrılır.

Seçim yalnız yeni Sentinel görüntüsü geldiğinde veya bu seçimin sürümü değiştiğinde
uygulanır. Böylece daha sonraki zaman-serisi/bulut tamamlama adayları aynı görüntüde
tekrar tekrar silinmez. Baz aday listesi değiştiğinde tamamlayıcı katmanların durum
cache'leri sıfırlanır; aynı akışta güvenli biçimde yeniden değerlendirilebilirler.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np

import satellite
from daily_report import ISTANBUL, REPORT_REGIONS, build_daily_report, ensure_daily_schema
from scanner import connect


SELECTION_VERSION = "construction-scale-quota-v3-diagonal-sidecar"
RAW_LIMIT = 1_000_000
CONSTRUCTION_SCALE_MIN_M2 = satellite.SMALL_HOTSPOT_MAX_M2
CONSTRUCTION_SCALE_MAX_M2 = 10_000
CONSTRUCTION_SCALE_QUOTA = 6
CONSTRUCTION_EARLY_QUOTA = 3
DIAGONAL_SIDECAR_QUOTA = 1
DIAGONAL_SIDECAR_MAX_PARENT_FRACTION = 0.35
DIAGONAL_SIDECAR_TAG = "DIYAGONAL_YAN_KUME"
_ORIGINAL_HOTSPOTS = satellite._hotspots


def _area(item):
    try:
        return max(float(item.get("alan_m2") or 0), 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _candidate_key(item):
    try:
        return (
            round(float(item.get("enlem")), 6),
            round(float(item.get("boylam")), 6),
            round(_area(item)),
        )
    except (TypeError, ValueError, AttributeError):
        return None


def _four_connected_components(mask):
    """Yalnız kenardan temas eden pikselleri aynı kümede tutar."""
    rows, columns = np.nonzero(mask)
    if not len(rows):
        return []

    height, width = mask.shape
    visited = np.zeros(mask.shape, dtype=bool)
    neighbors = ((-1, 0), (0, -1), (0, 1), (1, 0))
    components = []
    for seed_row, seed_col in zip(rows.tolist(), columns.tolist()):
        if visited[seed_row, seed_col]:
            continue
        visited[seed_row, seed_col] = True
        stack = [(seed_row, seed_col)]
        component = []
        while stack:
            row, column = stack.pop()
            component.append((row, column))
            for row_offset, col_offset in neighbors:
                next_row = row + row_offset
                next_col = column + col_offset
                if not (0 <= next_row < height and 0 <= next_col < width):
                    continue
                if not mask[next_row, next_col] or visited[next_row, next_col]:
                    continue
                visited[next_row, next_col] = True
                stack.append((next_row, next_col))
        components.append(component)
    return components


def _component_candidate(component, bbox, shape, pixel_area_m2):
    pixels = np.asarray(component, dtype="int32")
    centroid = pixels.mean(axis=0)
    representative = pixels[int(np.argmin(np.sum((pixels - centroid) ** 2, axis=1)))]
    row, column = int(representative[0]), int(representative[1])
    height, width = shape
    west, south, east, north = bbox
    latitude = north - (row + 0.5) / height * (north - south)
    longitude = west + (column + 0.5) / width * (east - west)
    return {
        "mahalle": satellite._nearest_place(latitude, longitude),
        "enlem": round(latitude, 6),
        "boylam": round(longitude, 6),
        "alan_m2": round(len(component) * pixel_area_m2),
        "sinyal": (
            "Geniş değişim kümesine yalnız köşeden bağlı parsel ölçekli "
            "yüzey/toprak değişimi adayı"
        ),
        "boyut_sinifi": "STANDART",
        "geometri_kaynagi": DIAGONAL_SIDECAR_TAG,
    }


def _diagonal_sidecar_candidates(change_mask, bbox, pixel_area_m2):
    """Geniş 8-komşu kümenin içine gömülen küçük 4-komşu parsel parçalarını çıkarır."""
    eight_components = satellite._connected_components(change_mask)
    if not eight_components:
        return []
    four_components = _four_connected_components(change_mask)

    parent_by_pixel = {}
    parent_areas = {}
    for parent_index, component in enumerate(eight_components):
        parent_area = len(component) * pixel_area_m2
        if parent_area <= CONSTRUCTION_SCALE_MAX_M2:
            continue
        parent_areas[parent_index] = parent_area
        for pixel in component:
            parent_by_pixel[pixel] = parent_index

    children_by_parent = {}
    for component in four_components:
        parent_index = parent_by_pixel.get(component[0])
        if parent_index is None:
            continue
        children_by_parent.setdefault(parent_index, []).append(component)

    sidecars = []
    for parent_index, children in children_by_parent.items():
        if len(children) < 2:
            continue
        parent_area = parent_areas[parent_index]
        child_areas = [len(component) * pixel_area_m2 for component in children]
        # En az bir ana gövde hâlâ geniş sınıfta kalmalı. Bu şart, 7-8 bin m² gibi
        # tek bir şantiye kümesinin iki orta parçaya bölünüp çoğalmasını engeller.
        if not any(area > CONSTRUCTION_SCALE_MAX_M2 for area in child_areas):
            continue
        for component, child_area in zip(children, child_areas):
            if not (
                CONSTRUCTION_SCALE_MIN_M2
                <= child_area
                <= CONSTRUCTION_SCALE_MAX_M2
            ):
                continue
            if child_area / parent_area > DIAGONAL_SIDECAR_MAX_PARENT_FRACTION:
                continue
            sidecars.append(
                _component_candidate(component, bbox, change_mask.shape, pixel_area_m2)
            )
    return sidecars


def _uncapped_hotspots(
    change_mask,
    bbox,
    pixel_area_m2,
    small_site_mask=None,
    limit=satellite.HOTSPOT_LIMIT,
    small_quota=satellite.SMALL_HOTSPOT_QUOTA,
):
    del limit, small_quota
    base = _ORIGINAL_HOTSPOTS(
        change_mask,
        bbox,
        pixel_area_m2,
        small_site_mask=small_site_mask,
        limit=RAW_LIMIT,
        small_quota=0,
    )
    sidecars = _diagonal_sidecar_candidates(change_mask, bbox, pixel_area_m2)
    return base + sidecars


def _uncapped_analysis(region_key, pair):
    original = satellite._hotspots
    satellite._hotspots = _uncapped_hotspots
    try:
        return satellite.analyze_sentinel_change(region_key, pair=pair)
    finally:
        satellite._hotspots = original


def _balanced_select(
    candidates,
    limit=satellite.HOTSPOT_LIMIT,
    small_quota=satellite.SMALL_HOTSPOT_QUOTA,
    construction_quota=CONSTRUCTION_SCALE_QUOTA,
    construction_early_quota=CONSTRUCTION_EARLY_QUOTA,
    diagonal_sidecar_quota=DIAGONAL_SIDECAR_QUOTA,
):
    """Toplam tavanı büyütmeden küçük + şantiye ölçeği + geniş denge seçimi yap."""
    ranked = sorted(
        [item for item in candidates if isinstance(item, dict)],
        key=lambda item: (
            -_area(item),
            float(item.get("enlem") or 0),
            float(item.get("boylam") or 0),
        ),
    )
    limit = max(int(limit), 0)
    if len(ranked) <= limit:
        return ranked

    small = [item for item in ranked if _area(item) < CONSTRUCTION_SCALE_MIN_M2]
    construction = [
        item for item in ranked
        if CONSTRUCTION_SCALE_MIN_M2 <= _area(item) <= CONSTRUCTION_SCALE_MAX_M2
    ]
    wide = [item for item in ranked if _area(item) > CONSTRUCTION_SCALE_MAX_M2]

    sidecars = [
        item for item in construction
        if item.get("geometri_kaynagi") == DIAGONAL_SIDECAR_TAG
    ]
    regular_construction = [
        item for item in construction
        if item.get("geometri_kaynagi") != DIAGONAL_SIDECAR_TAG
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
            _area(item),
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
            _area(item),
            float(item.get("enlem") or 0),
            float(item.get("boylam") or 0),
        ),
    )
    early_selected = construction_asc[:early_slots]
    selected.extend(early_selected)

    remaining_construction_slots = regular_slots - len(early_selected)
    if remaining_construction_slots > 0:
        early_keys = {
            key for key in map(_candidate_key, early_selected) if key is not None
        }
        upper_construction = [
            item for item in regular_construction
            if (key := _candidate_key(item)) is not None and key not in early_keys
        ]
        selected.extend(upper_construction[:remaining_construction_slots])

    remaining = limit - len(selected)
    selected.extend(wide[:remaining])

    if len(selected) < limit:
        selected_keys = {
            key for key in map(_candidate_key, selected) if key is not None
        }
        leftovers = [
            item for item in ranked
            if (key := _candidate_key(item)) is not None and key not in selected_keys
        ]
        selected.extend(leftovers[: limit - len(selected)])

    return sorted(selected[:limit], key=lambda item: _area(item), reverse=True)


def _bucket_counts(items):
    counts = {"kucuk": 0, "santiye_olcegi": 0, "genis": 0}
    for item in items:
        area = _area(item)
        if area < CONSTRUCTION_SCALE_MIN_M2:
            counts["kucuk"] += 1
        elif area <= CONSTRUCTION_SCALE_MAX_M2:
            counts["santiye_olcegi"] += 1
        else:
            counts["genis"] += 1
    return counts


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
    selected = _balanced_select(synthetic)
    counts = _bucket_counts(selected)
    assert len(selected) == satellite.HOTSPOT_LIMIT
    assert counts == {"kucuk": 6, "santiye_olcegi": 6, "genis": 12}, counts
    construction_areas = sorted(
        int(_area(candidate))
        for candidate in selected
        if CONSTRUCTION_SCALE_MIN_M2 <= _area(candidate) <= CONSTRUCTION_SCALE_MAX_M2
    )
    assert construction_areas == [1000, 1500, 2000, 5500, 6000, 6500], construction_areas

    with_sidecar = list(synthetic)
    with_sidecar.append(
        item(90, 1200, geometri_kaynagi=DIAGONAL_SIDECAR_TAG)
    )
    sidecar_selected = _balanced_select(with_sidecar)
    selected_sidecars = [
        candidate for candidate in sidecar_selected
        if candidate.get("geometri_kaynagi") == DIAGONAL_SIDECAR_TAG
    ]
    assert len(sidecar_selected) == satellite.HOTSPOT_LIMIT
    assert len(selected_sidecars) == 1, selected_sidecars
    assert _bucket_counts(sidecar_selected) == {
        "kucuk": 6,
        "santiye_olcegi": 6,
        "genis": 12,
    }

    # 11.000 m² ana gövdeye yalnız köşeden bağlı 1.200 m² yan küme, tek geniş
    # 8-komşu ebeveyn içinden kontrollü bir parsel adayı olarak ayrışmalı.
    diagonal = np.zeros((18, 18), dtype=bool)
    diagonal[1:12, 1:11] = True  # 110 piksel = 11.000 m²
    diagonal[12:15, 11:15] = True  # 12 piksel = 1.200 m², yalnız diyagonal temas
    geometry_candidates = _diagonal_sidecar_candidates(
        diagonal,
        [26.30, 38.20, 26.32, 38.22],
        100.0,
    )
    assert len(geometry_candidates) == 1, geometry_candidates
    assert geometry_candidates[0]["alan_m2"] == 1200, geometry_candidates

    # Ana gövdesi >10.000 m² kalmayan benzer iki orta parça ayrıştırılmamalı.
    balanced_split = np.zeros((18, 18), dtype=bool)
    balanced_split[1:7, 1:11] = True  # 6.000 m²
    balanced_split[7:12, 11:21 if balanced_split.shape[1] > 21 else 18] = False
    assert not _diagonal_sidecar_candidates(
        balanced_split,
        [26.30, 38.20, 26.32, 38.22],
        100.0,
    )

    scarce = [item(100, 300), item(101, 1500)]
    scarce.extend(item(110 + i, 12000 + i * 1000) for i in range(30))
    scarce_selected = _balanced_select(scarce)
    scarce_counts = _bucket_counts(scarce_selected)
    assert len(scarce_selected) == satellite.HOTSPOT_LIMIT
    assert scarce_counts["kucuk"] == 1
    assert scarce_counts["santiye_olcegi"] == 1
    assert scarce_counts["genis"] == 22

    short = [item(200, 300), item(201, 1800), item(202, 15000)]
    assert _balanced_select(short) == sorted(short, key=lambda x: _area(x), reverse=True)


def _ensure_state_table(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS uydu_aday_secim_surumu (
        bolge TEXT PRIMARY KEY,
        son_item TEXT NOT NULL,
        surum TEXT NOT NULL,
        ham_aday INTEGER DEFAULT 0,
        secilen_aday INTEGER DEFAULT 0,
        santiye_olcegi_aday INTEGER DEFAULT 0,
        guncelleme TEXT NOT NULL)"""
    )


def _reset_complementary_state(connection, region_key):
    for table_name in ("uydu_zaman_serisi", "uydu_son_bulut_boslugu"):
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table_name,),
        ).fetchone()
        if exists:
            connection.execute(f"DELETE FROM {table_name} WHERE bolge=?", (region_key,))


def rebalance_candidates():
    ensure_daily_schema()
    _self_check()
    report_date = datetime.now(ISTANBUL).strftime("%Y-%m-%d")
    changed = []
    skipped = []
    errors = []

    with connect() as connection:
        _ensure_state_table(connection)
        for region_key in REPORT_REGIONS:
            try:
                row = connection.execute(
                    """SELECT son_item,hareket_json,hata FROM gunluk_uydu_raporlari
                    WHERE rapor_tarihi=? AND bolge=? LIMIT 1""",
                    (report_date, region_key),
                ).fetchone()
                if not row or row[2] or not row[0]:
                    skipped.append(region_key)
                    continue

                latest_item = str(row[0])
                state = connection.execute(
                    """SELECT son_item,surum FROM uydu_aday_secim_surumu
                    WHERE bolge=? LIMIT 1""",
                    (region_key,),
                ).fetchone()
                if state and state[0] == latest_item and state[1] == SELECTION_VERSION:
                    skipped.append(region_key)
                    continue

                pair = satellite.sentinel_pair(region_key)
                if pair[1].get("id") != latest_item:
                    skipped.append(region_key)
                    continue

                raw_result = _uncapped_analysis(region_key, pair)
                raw = [
                    item for item in raw_result.get("hotspots", [])
                    if isinstance(item, dict)
                ]
                selected = _balanced_select(raw)
                counts = _bucket_counts(selected)

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
                _reset_complementary_state(connection, region_key)
                connection.execute(
                    """INSERT INTO uydu_aday_secim_surumu
                    (bolge,son_item,surum,ham_aday,secilen_aday,santiye_olcegi_aday,guncelleme)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(bolge) DO UPDATE SET
                    son_item=excluded.son_item,surum=excluded.surum,
                    ham_aday=excluded.ham_aday,secilen_aday=excluded.secilen_aday,
                    santiye_olcegi_aday=excluded.santiye_olcegi_aday,
                    guncelleme=excluded.guncelleme""",
                    (
                        region_key,
                        latest_item,
                        SELECTION_VERSION,
                        len(raw),
                        len(selected),
                        counts["santiye_olcegi"],
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                changed.append((region_key, len(raw), len(selected), counts))
            except Exception as exc:
                errors.append(f"{region_key}: {type(exc).__name__}: {exc}")

    if changed:
        build_daily_report()
    return changed, skipped, errors


def main():
    _self_check()
    changed, skipped, errors = rebalance_candidates()
    if changed:
        detail = " | ".join(
            f"{region}: ham {raw} → {selected}; 250-800={counts['kucuk']}, "
            f"800-10000={counts['santiye_olcegi']}, >10000={counts['genis']}"
            for region, raw, selected, counts in changed
        )
        print("Uydu aday dengesi: " + detail)
    else:
        print("Uydu aday dengesi güncel; yeniden seçim gerekmedi.")
    if skipped:
        print("Atlanan/güncel bölgeler: " + ", ".join(skipped))
    if errors:
        raise RuntimeError("Uydu aday dengeleme hatası: " + " | ".join(errors))


if __name__ == "__main__":
    main()
