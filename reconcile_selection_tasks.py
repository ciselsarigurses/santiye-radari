"""Aynı Sentinel görüntüsünde seçim politikası değişince oluşan görev şişmesini temizler.

Güncel raporda artık seçilmeyen bir uydu görevi yalnızca aynı gün otomasyon tarafından
oluşturulduysa, hiç saha işlemi görmediyse ve hâlâ KONTROLE_GIT durumundaysa
ALGORITMA_ELENDI yapılır. Önceki günlerden bekleyen görevler, TEKRAR_GIT/
KONTROL_EDILDI kararları ve kullanıcı dokunmuş kayıtlar korunur.

Ayrıca aynı gün içinde yeni ve taze Sentinel seçiminin en fazla 5 m yanında kalan,
aynı bölgedeki ikinci açık uydu görevi MUKERRER olarak ayrılır. Bu dar geometri
koruması ``son_islem`` saat biçimine bağlı değildir; böylece otomasyonun zaman damgalı
bir kaydı 0 m² BEKLEYEN görev olarak operasyon kuyruğunda tutması engellenir. Asıl
güncel görev açık kalır, kayıt silinmez ve gerçek komşu küçük sahaları birleştirmemek
için 25–80 m normal eşleşme toleransları bu yolda kullanılmaz.

Bu katman yeni alarm üretmez. Rebalance gibi aynı görüntü üzerinde 24 adayın içeriği
değiştiğinde eski ve yeni seçimin aynı anda açık görev sayısını şişirmesini önler.
ALGORITMA_ELENDI kayıt güncel bir analizde yeniden seçilirse mevcut
reactivate_current_satellite.py tarafından tekrar açılabilir.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime

from daily_report import ISTANBUL, _report_hotspots, ensure_daily_schema
from field_state import (
    INTERNAL_DUPLICATE_STATUS,
    INTERNAL_SUPERSEDED_STATUS,
    ensure_state_schema,
    sync_satellite_tasks,
)
from report_quality import normalize_daily_report
from scanner import connect


SAME_LOCATION_DUPLICATE_METERS = 5.0


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _same_day_superseded_ids(connection, current_task_ids, current_sources, report_date):
    current_task_ids = {str(value) for value in current_task_ids if str(value)}
    current_sources = sorted({str(value) for value in current_sources if str(value)})
    if not current_sources:
        return []

    placeholders = ",".join("?" for _ in current_sources)
    rows = connection.execute(
        f"""SELECT gorev_id FROM saha_durumlari
        WHERE kaynak='uydu'
        AND kaynak_kimlik IN ({placeholders})
        AND durum='KONTROLE_GIT'
        AND COALESCE(kontrol_sayisi,0)=0
        AND substr(COALESCE(ilk_gorulme,''),1,10)=?
        AND substr(COALESCE(son_gorulme,''),1,10)=?
        AND COALESCE(son_islem,'')=?""",
        (*current_sources, report_date, report_date, report_date),
    ).fetchall()
    return [
        str(row[0]) for row in rows
        if str(row[0] or "") and str(row[0]) not in current_task_ids
    ]


def _same_day_same_location_duplicates(
    connection,
    current,
    report_date,
    max_distance_m=SAME_LOCATION_DUPLICATE_METERS,
):
    """Taze güncel görevin <=5 m yanındaki aynı-gün ikinci görevi güvenle bul.

    ``son_islem`` burada bilinçli olarak filtre değildir: reaktivasyon gibi otomatik
    yollar aynı gün içinde saatli UTC damgası yazabilir. Dar <=5 m eşleşme ve tek bir
    taze güncel karşılık şartı, kullanıcı dokunuşunu geniş bir seçim-dışı filtresine
    katmadan operasyonel kopyayı ayırır. TEKRAR_GIT/KONTROL_EDILDI ve kontrol görmüş
    kayıtlar sorguya hiç girmez.
    """
    fresh_current = []
    current_ids = set()
    for item in current:
        if not isinstance(item, dict) or item.get("yeni_goruntu") is not True:
            continue
        task_id = str(item.get("gorev_id") or "")
        source = str(item.get("bolge") or "")
        try:
            latitude = float(item.get("enlem"))
            longitude = float(item.get("boylam"))
            area_m2 = float(item.get("alan_m2") or 0)
        except (TypeError, ValueError):
            continue
        if not task_id or not source or area_m2 <= 0:
            continue
        current_ids.add(task_id)
        fresh_current.append(
            {
                "gorev_id": task_id,
                "kaynak_kimlik": source,
                "enlem": latitude,
                "boylam": longitude,
            }
        )

    sources = sorted({item["kaynak_kimlik"] for item in fresh_current})
    if not sources:
        return []

    placeholders = ",".join("?" for _ in sources)
    rows = connection.execute(
        f"""SELECT gorev_id,kaynak_kimlik,enlem,boylam FROM saha_durumlari
        WHERE kaynak='uydu'
        AND kaynak_kimlik IN ({placeholders})
        AND durum='KONTROLE_GIT'
        AND COALESCE(kontrol_sayisi,0)=0
        AND substr(COALESCE(ilk_gorulme,''),1,10)=?
        AND substr(COALESCE(son_gorulme,''),1,10)=?
        AND enlem IS NOT NULL AND boylam IS NOT NULL""",
        (*sources, report_date, report_date),
    ).fetchall()

    duplicates = []
    for task_id, source, latitude, longitude in rows:
        task_id = str(task_id or "")
        source = str(source or "")
        if not task_id or task_id in current_ids:
            continue
        try:
            old_point = (float(latitude), float(longitude))
        except (TypeError, ValueError):
            continue

        matches = []
        for item in fresh_current:
            if item["kaynak_kimlik"] != source:
                continue
            distance = _distance_m(
                old_point,
                (item["enlem"], item["boylam"]),
            )
            if distance <= float(max_distance_m):
                matches.append((distance, item["gorev_id"]))

        # Birden fazla güncel görev 5 m içinde ise hangisinin gerçek karşılık
        # olduğunu varsayma; saha/sonraki görüntü ayrımına bırak.
        if len(matches) != 1:
            continue
        distance, current_task_id = matches[0]
        duplicates.append(
            {
                "eski_gorev_id": task_id,
                "guncel_gorev_id": current_task_id,
                "mesafe_m": round(distance, 1),
            }
        )

    duplicates.sort(key=lambda item: (item["mesafe_m"], item["eski_gorev_id"]))
    return duplicates


def _self_check():
    connection = sqlite3.connect(":memory:")
    ensure_state_schema(connection)
    source = "Çeşme merkez · Alaçatı · Ilıca"
    rows = [
        # güncel görev: korunmalı
        ("UCURRENT", source, 38.315537, 26.486671, "KONTROLE_GIT", 0, "2026-08-29", "2026-08-29", "2026-08-29"),
        # aynı gün otomatik açılmış, artık seçim dışı: kapanmalı
        ("UOLD", source, 38.250000, 26.400000, "KONTROLE_GIT", 0, "2026-08-29", "2026-08-29", "2026-08-29"),
        # saatli otomasyon damgalı ama güncel görevle ~2 m aynı konum: mükerrer olmalı
        ("UOLDSTAMP", source, 38.315537, 26.486698, "KONTROLE_GIT", 0, "2026-08-29", "2026-08-29", "2026-08-29 06:10 UTC"),
        # ~10 m komşu gerçek saha ihtimali: dar mükerrer koruması dokunmamalı
        ("UNEIGHBOR", source, 38.315627, 26.486671, "KONTROLE_GIT", 0, "2026-08-29", "2026-08-29", "2026-08-29 06:11 UTC"),
        # önceki günden bekleyen gerçek görev: korunmalı
        ("UPREV", source, 38.315537, 26.486698, "KONTROLE_GIT", 0, "2026-08-28", "2026-08-28", "2026-08-28"),
        # kullanıcı/saha işlemi görmüş görev: korunmalı
        ("UTOUCHED", source, 38.315537, 26.486698, "KONTROLE_GIT", 1, "2026-08-29", "2026-08-29", "2026-08-29 06:12 UTC"),
        # tekrar-git kararı: korunmalı
        ("UREPEAT", source, 38.315537, 26.486698, "TEKRAR_GIT", 1, "2026-08-29", "2026-08-29", "2026-08-29 06:13 UTC"),
    ]
    connection.executemany(
        """INSERT INTO saha_durumlari
        (gorev_id,kaynak,kaynak_kimlik,enlem,boylam,durum,kontrol_sayisi,
        ilk_gorulme,son_gorulme,son_islem)
        VALUES(?,'uydu',?,?,?,?,?,?,?,?)""",
        rows,
    )
    ids = _same_day_superseded_ids(
        connection, {"UCURRENT"}, {source}, "2026-08-29"
    )
    assert ids == ["UOLD"], ids

    current = [
        {
            "gorev_id": "UCURRENT",
            "bolge": source,
            "enlem": 38.315537,
            "boylam": 26.486671,
            "alan_m2": 32_803,
            "yeni_goruntu": True,
        }
    ]
    duplicates = _same_day_same_location_duplicates(
        connection, current, "2026-08-29"
    )
    assert [item["eski_gorev_id"] for item in duplicates] == ["UOLDSTAMP"], duplicates
    assert duplicates[0]["mesafe_m"] < SAME_LOCATION_DUPLICATE_METERS

    stale_current = [{**current[0], "yeni_goruntu": False}]
    assert not _same_day_same_location_duplicates(
        connection, stale_current, "2026-08-29"
    )
    connection.close()


def reconcile_selection_tasks(report_date=None):
    ensure_daily_schema()
    _self_check()
    report_date = report_date or datetime.now(ISTANBUL).strftime("%Y-%m-%d")
    retired = []
    duplicate_ids = []

    with connect() as connection:
        ensure_state_schema(connection)
        raw = _report_hotspots(connection, report_date)
        current = sync_satellite_tasks(connection, raw, report_date)
        current_ids = {
            str(item.get("gorev_id") or "")
            for item in current
            if str(item.get("gorev_id") or "")
        }
        current_sources = {
            str(item.get("bolge") or "")
            for item in current
            if str(item.get("bolge") or "")
        }

        # Önce yalnız fiziksel olarak aynı noktayı temsil eden dar kopyayı ayır.
        # Burada son_islem biçimine güvenilmez; bir güncel taze görev açık kalır.
        duplicate_pairs = _same_day_same_location_duplicates(
            connection, current, report_date
        )
        for duplicate in duplicate_pairs:
            task_id = duplicate["eski_gorev_id"]
            cursor = connection.execute(
                """UPDATE saha_durumlari SET durum=?
                WHERE gorev_id=? AND durum='KONTROLE_GIT'
                AND COALESCE(kontrol_sayisi,0)=0
                AND substr(COALESCE(ilk_gorulme,''),1,10)=?
                AND substr(COALESCE(son_gorulme,''),1,10)=?""",
                (
                    INTERNAL_DUPLICATE_STATUS,
                    task_id,
                    report_date,
                    report_date,
                ),
            )
            if cursor.rowcount:
                duplicate_ids.append(task_id)

        candidates = _same_day_superseded_ids(
            connection, current_ids, current_sources, report_date
        )
        for task_id in candidates:
            # Dar mükerrer yolu bu kaydı zaten ayırdıysa ikinci durum yazma.
            if task_id in duplicate_ids:
                continue
            cursor = connection.execute(
                """UPDATE saha_durumlari SET durum=?
                WHERE gorev_id=? AND durum='KONTROLE_GIT'
                AND COALESCE(kontrol_sayisi,0)=0
                AND substr(COALESCE(ilk_gorulme,''),1,10)=?
                AND substr(COALESCE(son_gorulme,''),1,10)=?
                AND COALESCE(son_islem,'')=?""",
                (
                    INTERNAL_SUPERSEDED_STATUS,
                    task_id,
                    report_date,
                    report_date,
                    report_date,
                ),
            )
            if cursor.rowcount:
                retired.append(task_id)

    changed = duplicate_ids + retired
    if changed:
        # Aktif görev sayısını ve kullanıcıya açık JSON/Markdown raporunu aynı
        # işlem içinde tekrar üret; böylece geçici görev şişmesi dışarı sızmaz.
        normalize_daily_report()
    return changed


def main():
    task_ids = reconcile_selection_tasks()
    if task_ids:
        print(
            "Aynı gün aynı-konum mükerreri veya seçim dışı kalan güvenli uydu "
            f"görevleri pasifleştirildi: {len(task_ids)}"
        )
    else:
        print("Aynı gün seçim değişiminden kalan fazladan açık uydu görevi yok.")


if __name__ == "__main__":
    main()
