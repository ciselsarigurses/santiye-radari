"""Saha kontrollerinin yapılandırılmış sonucunu ayrı tabloda saklar.

Mevcut saha durum mekanizmasına dokunmadan, doğrulanmış saha geri bildirimlerini
gelecekte yanlış pozitifleri azaltmak için kullanılabilecek biçimde toplar.
"""

from __future__ import annotations

from datetime import datetime, timezone

from scanner import connect


ALLOWED_OUTCOMES = {
    "SANTIYE_KAZI",
    "YOL_ALTYAPI",
    "TARLA_BITKI",
    "YANLIS_POZITIF",
}

OUTCOME_LABELS = {
    "SANTIYE_KAZI": "Gerçek şantiye / kazı / temel",
    "YOL_ALTYAPI": "Yol / altyapı çalışması",
    "TARLA_BITKI": "Tarla / bitki değişimi",
    "YANLIS_POZITIF": "Yanlış pozitif / başka neden",
}


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def ensure_outcome_schema(connection=None):
    owns_connection = connection is None
    connection = connection or connect()
    try:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS saha_sonuclari (
            gorev_id TEXT PRIMARY KEY,
            sonuc TEXT NOT NULL,
            kayit_zamani TEXT NOT NULL)"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_saha_sonuc ON saha_sonuclari(sonuc)"
        )
        if owns_connection:
            connection.commit()
    finally:
        if owns_connection:
            connection.close()


def save_outcome(task_id, outcome):
    task_id = str(task_id or "").strip().upper()
    outcome = str(outcome or "").strip().upper()
    if outcome not in ALLOWED_OUTCOMES:
        raise ValueError("Bilinmeyen saha kontrol sonucu.")

    with connect() as connection:
        ensure_outcome_schema(connection)
        connection.execute(
            """INSERT INTO saha_sonuclari(gorev_id,sonuc,kayit_zamani)
            VALUES(?,?,?)
            ON CONFLICT(gorev_id) DO UPDATE SET
            sonuc=excluded.sonuc,kayit_zamani=excluded.kayit_zamani""",
            (task_id, outcome, _now()),
        )
    return outcome


def clear_outcome(task_id):
    task_id = str(task_id or "").strip().upper()
    with connect() as connection:
        ensure_outcome_schema(connection)
        connection.execute("DELETE FROM saha_sonuclari WHERE gorev_id=?", (task_id,))


def outcome_map():
    with connect() as connection:
        ensure_outcome_schema(connection)
        rows = connection.execute(
            "SELECT gorev_id,sonuc,kayit_zamani FROM saha_sonuclari"
        ).fetchall()
    return {
        str(task_id): {
            "sonuc": str(outcome),
            "etiket": OUTCOME_LABELS.get(str(outcome), str(outcome)),
            "kayit_zamani": recorded_at,
        }
        for task_id, outcome, recorded_at in rows
    }
