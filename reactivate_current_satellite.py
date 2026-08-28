"""Güncel uydu analizinde yeniden görünen algoritma-elendi görevlerini geri açar.

Bir aday yalnızca önceki bir analiz sürümünde otomatik olarak
``ALGORITMA_ELENDI`` yapılmışsa ve hiçbir saha işlemi görmemişse yeniden açılır.
Kullanıcının ``KONTROL_EDILDI`` / ``TEKRAR_GIT`` kararlarına dokunulmaz;
``MUKERRER`` kayıtlar da otomatik olarak geri açılmaz.
"""

from __future__ import annotations

from datetime import datetime, timezone

from daily_report import ISTANBUL, _report_hotspots, ensure_daily_schema
from field_state import INTERNAL_SUPERSEDED_STATUS, sync_satellite_tasks
from scanner import connect


def _now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def reactivate_current(report_date=None):
    """Güncel analizde tekrar bulunan güvenli otomatik-elendi görevleri geri aç."""
    ensure_daily_schema()
    report_date = report_date or datetime.now(ISTANBUL).strftime("%Y-%m-%d")
    reactivated = []

    with connect() as connection:
        raw = _report_hotspots(connection, report_date)
        current = sync_satellite_tasks(connection, raw, report_date)

        for item in current:
            if str(item.get("saha_durumu") or "") != INTERNAL_SUPERSEDED_STATUS:
                continue
            task_id = str(item.get("gorev_id") or "")
            if not task_id:
                continue
            cursor = connection.execute(
                """UPDATE saha_durumlari
                SET durum='KONTROLE_GIT', son_islem=?
                WHERE gorev_id=? AND durum=?
                AND COALESCE(kontrol_sayisi,0)=0""",
                (_now_utc(), task_id, INTERNAL_SUPERSEDED_STATUS),
            )
            if cursor.rowcount:
                reactivated.append(task_id)

    return reactivated


if __name__ == "__main__":
    task_ids = reactivate_current()
    if task_ids:
        print(
            "Güncel uydu analizinde yeniden desteklenen otomatik-elendi görevler "
            "geri açıldı: " + ", ".join(task_ids)
        )
    else:
        print("Geri açılması gereken güncel otomatik-elendi uydu görevi yok.")
