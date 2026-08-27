"""Arama motoru sıralama dalgalanmasında taze internet adaylarını kısa süre korur."""

from datetime import datetime, timedelta, timezone

from scanner import connect, ensure_schema


RETENTION_DAYS = 3


def retain_recent_candidates(days=RETENTION_DAYS):
    """Son birkaç günde görülmüş adayları tek bir aramada kayboldu diye pasife düşürme."""
    ensure_schema()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    with connect() as connection:
        cursor = connection.execute(
            """UPDATE internet_adaylari
            SET aktif=1
            WHERE aktif=0
              AND kaynak_tipi IS NOT NULL
              AND son_gorulme IS NOT NULL
              AND son_gorulme>=?""",
            (cutoff,),
        )
        return max(cursor.rowcount or 0, 0)


if __name__ == "__main__":
    restored = retain_recent_candidates()
    print(
        f"Aday kalıcılığı: son {RETENTION_DAYS} günde görülmüş "
        f"{restored} kayıt aktif tutuldu."
    )
