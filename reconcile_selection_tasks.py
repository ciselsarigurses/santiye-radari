"""Aynı Sentinel görüntüsünde seçim politikası değişince oluşan görev şişmesini temizler.

Güncel raporda artık seçilmeyen bir uydu görevi yalnızca aynı gün otomasyon tarafından
oluşturulduysa, hiç saha işlemi görmediyse ve hâlâ KONTROLE_GIT durumundaysa
ALGORITMA_ELENDI yapılır. Önceki günlerden bekleyen görevler, TEKRAR_GIT/
KONTROL_EDILDI kararları ve kullanıcı dokunmuş kayıtlar korunur.

Bu katman yeni alarm üretmez. Rebalance gibi aynı görüntü üzerinde 24 adayın içeriği
değiştiğinde eski ve yeni seçimin aynı anda açık görev sayısını şişirmesini önler.
ALGORITMA_ELENDI kayıt güncel bir analizde yeniden seçilirse mevcut
reactivate_current_satellite.py tarafından tekrar açılabilir.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from daily_report import ISTANBUL, _report_hotspots, ensure_daily_schema
from field_state import INTERNAL_SUPERSEDED_STATUS, ensure_state_schema, sync_satellite_tasks
from report_quality import normalize_daily_report
from scanner import connect


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


def _self_check():
    connection = sqlite3.connect(":memory:")
    ensure_state_schema(connection)
    source = "Çeşme merkez · Alaçatı · Ilıca"
    rows = [
        # güncel görev: korunmalı
        ("UCURRENT", source, "KONTROLE_GIT", 0, "2026-08-29", "2026-08-29", "2026-08-29"),
        # aynı gün otomatik açılmış, artık seçim dışı: kapanmalı
        ("UOLD", source, "KONTROLE_GIT", 0, "2026-08-29", "2026-08-29", "2026-08-29"),
        # önceki günden bekleyen gerçek görev: korunmalı
        ("UPREV", source, "KONTROLE_GIT", 0, "2026-08-28", "2026-08-28", "2026-08-28"),
        # kullanıcı/saha işlemi görmüş görev: korunmalı
        ("UTOUCHED", source, "KONTROLE_GIT", 1, "2026-08-29", "2026-08-29", "2026-08-29"),
        # tekrar-git kararı: korunmalı
        ("UREPEAT", source, "TEKRAR_GIT", 1, "2026-08-29", "2026-08-29", "2026-08-29"),
    ]
    connection.executemany(
        """INSERT INTO saha_durumlari
        (gorev_id,kaynak,kaynak_kimlik,durum,kontrol_sayisi,
        ilk_gorulme,son_gorulme,son_islem)
        VALUES(?,'uydu',?,?,?,?,?,?)""",
        rows,
    )
    ids = _same_day_superseded_ids(
        connection, {"UCURRENT"}, {source}, "2026-08-29"
    )
    assert ids == ["UOLD"], ids
    connection.close()


def reconcile_selection_tasks(report_date=None):
    ensure_daily_schema()
    _self_check()
    report_date = report_date or datetime.now(ISTANBUL).strftime("%Y-%m-%d")
    retired = []

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
        candidates = _same_day_superseded_ids(
            connection, current_ids, current_sources, report_date
        )
        for task_id in candidates:
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

    if retired:
        # Aktif görev sayısını ve kullanıcıya açık JSON/Markdown raporunu aynı
        # işlem içinde tekrar üret; böylece geçici görev şişmesi dışarı sızmaz.
        normalize_daily_report()
    return retired


def main():
    task_ids = reconcile_selection_tasks()
    if task_ids:
        print(
            "Aynı gün seçim dışı kalan, saha işlemi görmemiş uydu görevleri "
            f"pasifleştirildi: {len(task_ids)}"
        )
    else:
        print("Aynı gün seçim değişiminden kalan fazladan açık uydu görevi yok.")


if __name__ == "__main__":
    main()
