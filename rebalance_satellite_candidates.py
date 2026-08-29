"""Sentinel 24-aday tavanında şantiye ölçeğindeki kümeler için kontrollü temsil sağlar.

Ana uydu motorunun 250 m², yaklaşık 10 m ve spektral eşiklerini değiştirmez. Yalnız
ana motorun aynı görüntüde ürettiği tüm geçerli kümeler arasından 24 kayıt seçilirken
250-800 m² güçlü küçük saha adaylarını korur, 800-10.000 m² aralığına sınırlı bir kota
ayırır ve kalan kapasiteyi geniş değişimlere bırakır. Toplam aday tavanı artmaz.

Seçim yalnız yeni Sentinel görüntüsü geldiğinde veya bu seçimin sürümü değiştiğinde
uygulanır. Böylece daha sonraki zaman-serisi/bulut tamamlama adayları aynı görüntüde
tekrar tekrar silinmez. Baz aday listesi değiştiğinde tamamlayıcı katmanların durum
cache'leri sıfırlanır; aynı akışta güvenli biçimde yeniden değerlendirilebilirler.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import satellite
from daily_report import ISTANBUL, REPORT_REGIONS, build_daily_report, ensure_daily_schema
from scanner import connect


SELECTION_VERSION = "construction-scale-quota-v1"
RAW_LIMIT = 1_000_000
CONSTRUCTION_SCALE_MIN_M2 = satellite.SMALL_HOTSPOT_MAX_M2
CONSTRUCTION_SCALE_MAX_M2 = 10_000
CONSTRUCTION_SCALE_QUOTA = 6
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


def _uncapped_hotspots(
    change_mask,
    bbox,
    pixel_area_m2,
    small_site_mask=None,
    limit=satellite.HOTSPOT_LIMIT,
    small_quota=satellite.SMALL_HOTSPOT_QUOTA,
):
    del limit, small_quota
    return _ORIGINAL_HOTSPOTS(
        change_mask,
        bbox,
        pixel_area_m2,
        small_site_mask=small_site_mask,
        limit=RAW_LIMIT,
        small_quota=0,
    )


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

    selected = []
    selected.extend(small[: min(max(int(small_quota), 0), limit)])

    remaining = limit - len(selected)
    selected.extend(
        construction[: min(max(int(construction_quota), 0), remaining)]
    )

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
    def item(index, area):
        return {
            "enlem": 38.20 + index * 0.0001,
            "boylam": 26.30 + index * 0.0001,
            "alan_m2": area,
        }

    synthetic = []
    synthetic.extend(item(i, 300 + i * 50) for i in range(6))
    synthetic.extend(item(20 + i, 1000 + i * 500) for i in range(12))
    synthetic.extend(item(50 + i, 11000 + i * 1000) for i in range(30))
    selected = _balanced_select(synthetic)
    counts = _bucket_counts(selected)
    assert len(selected) == satellite.HOTSPOT_LIMIT
    assert counts == {"kucuk": 6, "santiye_olcegi": 6, "genis": 12}, counts

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
