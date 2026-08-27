"""Arama sıralaması dalgalanmasında yalnızca bir tarama kaçıran adayları korur."""

from scanner import connect, ensure_schema


def retain_recent_candidates():
    """Önceki taramada görülüp bu turda kaybolan adayı bir tur daha aktif tut."""
    ensure_schema()
    with connect() as connection:
        scans = connection.execute(
            """SELECT baslangic FROM tarama_gecmisi
            ORDER BY id DESC LIMIT 2"""
        ).fetchall()
        if len(scans) < 2:
            return 0

        current_started = scans[0][0]
        previous_started = scans[1][0]
        if not current_started or not previous_started:
            return 0

        cursor = connection.execute(
            """UPDATE internet_adaylari
            SET aktif=1
            WHERE aktif=0
              AND kaynak_tipi IS NOT NULL
              AND son_gorulme IS NOT NULL
              AND son_gorulme>=?
              AND son_gorulme<?""",
            (previous_started, current_started),
        )
        return max(cursor.rowcount or 0, 0)


if __name__ == "__main__":
    restored = retain_recent_candidates()
    print(
        "Aday kalıcılığı: önceki taramada görülüp bu turda kaybolan "
        f"{restored} kayıt bir tur daha aktif tutuldu."
    )
