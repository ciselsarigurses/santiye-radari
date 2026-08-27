"""Saha görevlerinin kalıcı durumunu SQLite içinde yönetir.

Durumlar GitHub Actions üzerinden repository'deki ``santiye.db`` dosyasına
yazılır. Böylece Streamlit Community Cloud'un geçici yerel diski kullanıcı
kararlarının tek kaynağı olmaz.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone

from scanner import connect


ALLOWED_STATUSES = {"KONTROLE_GIT", "TEKRAR_GIT", "KONTROL_EDILDI"}
SATELLITE_MATCH_METERS = 80


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def ensure_state_schema(connection=None):
    owns_connection = connection is None
    connection = connection or connect()
    try:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS saha_durumlari (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gorev_id TEXT UNIQUE NOT NULL,
            kaynak TEXT NOT NULL,
            kaynak_kimlik TEXT,
            mahalle TEXT,
            enlem REAL,
            boylam REAL,
            durum TEXT DEFAULT 'KONTROLE_GIT',
            kontrol_sayisi INTEGER DEFAULT 0,
            ilk_gorulme TEXT,
            son_gorulme TEXT,
            son_islem TEXT)"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_saha_durum ON saha_durumlari(durum)"
        )
        if owns_connection:
            connection.commit()
    finally:
        if owns_connection:
            connection.close()


def satellite_task_id(item):
    """Yaklaşık 100 m hücreye dayalı, koordinatı açığa çıkarmayan ilk kimlik."""
    latitude = float(item.get("enlem"))
    longitude = float(item.get("boylam"))
    region = str(item.get("bolge") or "uydu")
    grid_key = f"{region}|{latitude:.3f}|{longitude:.3f}"
    digest = hashlib.sha1(grid_key.encode("utf-8")).hexdigest()[:10].upper()
    return f"U{digest}"


def site_task_id(site_id):
    return f"S{int(site_id)}"


def _distance_m(lat1, lon1, lat2, lon2):
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _nearby_satellite_task(connection, source_key, latitude, longitude):
    """Yeni görüntüde merkezi biraz kayan aynı saha görevini bulur."""
    rows = connection.execute(
        """SELECT gorev_id,enlem,boylam FROM saha_durumlari
        WHERE kaynak='uydu' AND kaynak_kimlik=?
        AND enlem IS NOT NULL AND boylam IS NOT NULL""",
        (source_key,),
    ).fetchall()
    nearest_id = None
    nearest_distance = None
    for task_id, old_latitude, old_longitude in rows:
        try:
            distance = _distance_m(
                latitude,
                longitude,
                float(old_latitude),
                float(old_longitude),
            )
        except (TypeError, ValueError):
            continue
        if nearest_distance is None or distance < nearest_distance:
            nearest_id = str(task_id)
            nearest_distance = distance
    if nearest_distance is not None and nearest_distance <= SATELLITE_MATCH_METERS:
        return nearest_id
    return None


def _upsert_task(
    connection,
    *,
    task_id,
    source,
    source_key,
    neighborhood,
    latitude,
    longitude,
    seen_at,
):
    existing = connection.execute(
        "SELECT durum FROM saha_durumlari WHERE gorev_id=?", (task_id,)
    ).fetchone()
    if existing:
        connection.execute(
            """UPDATE saha_durumlari SET kaynak=?,kaynak_kimlik=?,mahalle=?,
            enlem=?,boylam=?,son_gorulme=? WHERE gorev_id=?""",
            (
                source,
                source_key,
                neighborhood,
                latitude,
                longitude,
                seen_at,
                task_id,
            ),
        )
        return str(existing[0] or "KONTROLE_GIT")

    connection.execute(
        """INSERT INTO saha_durumlari
        (gorev_id,kaynak,kaynak_kimlik,mahalle,enlem,boylam,durum,
        kontrol_sayisi,ilk_gorulme,son_gorulme,son_islem)
        VALUES(?,?,?,?,?,?,'KONTROLE_GIT',0,?,?,?)""",
        (
            task_id,
            source,
            source_key,
            neighborhood,
            latitude,
            longitude,
            seen_at,
            seen_at,
            seen_at,
        ),
    )
    return "KONTROLE_GIT"


def sync_satellite_tasks(connection, hotspots, report_date):
    """Günlük uydu adaylarını durum tablosuna ekler; mevcut kararı bozmaz."""
    ensure_state_schema(connection)
    decorated = []
    for item in hotspots:
        try:
            latitude = float(item.get("enlem"))
            longitude = float(item.get("boylam"))
        except (TypeError, ValueError):
            continue
        source_key = str(item.get("bolge") or "")
        generated_id = satellite_task_id(item)
        exact = connection.execute(
            "SELECT 1 FROM saha_durumlari WHERE gorev_id=?",
            (generated_id,),
        ).fetchone()
        task_id = generated_id if exact else (
            _nearby_satellite_task(connection, source_key, latitude, longitude)
            or generated_id
        )
        status = _upsert_task(
            connection,
            task_id=task_id,
            source="uydu",
            source_key=source_key,
            neighborhood=str(item.get("mahalle") or ""),
            latitude=latitude,
            longitude=longitude,
            seen_at=report_date,
        )
        updated = dict(item)
        updated["gorev_id"] = task_id
        updated["saha_durumu"] = status
        decorated.append(updated)
    return decorated


def sync_site_tasks(connection, seen_at=None):
    """Ana saha listesindeki kayıtları aynı durum mekanizmasına bağlar."""
    ensure_state_schema(connection)
    seen_at = seen_at or _now()
    rows = connection.execute(
        """SELECT id,mahalle,enlem,boylam,aktif FROM santiyeler ORDER BY id"""
    ).fetchall()
    for site_id, neighborhood, latitude, longitude, active in rows:
        task_id = site_task_id(site_id)
        status = _upsert_task(
            connection,
            task_id=task_id,
            source="saha",
            source_key=str(site_id),
            neighborhood=str(neighborhood or ""),
            latitude=latitude,
            longitude=longitude,
            seen_at=seen_at,
        )
        # Önceden ana listede arşivlenmiş kayıtları tekrar açma.
        if not active and status != "KONTROL_EDILDI":
            connection.execute(
                "UPDATE saha_durumlari SET durum='KONTROL_EDILDI' WHERE gorev_id=?",
                (task_id,),
            )


def apply_status(task_id, status):
    status = str(status or "").strip().upper()
    task_id = str(task_id or "").strip().upper()
    if status not in ALLOWED_STATUSES:
        raise ValueError("Bilinmeyen saha durumu.")

    with connect() as connection:
        ensure_state_schema(connection)
        row = connection.execute(
            """SELECT kaynak,kaynak_kimlik,durum,kontrol_sayisi
            FROM saha_durumlari WHERE gorev_id=?""",
            (task_id,),
        ).fetchone()
        if not row:
            raise ValueError("Saha görevi bulunamadı veya henüz günlük rapora girmedi.")

        source, source_key, old_status, count = row
        increment = 1 if status in {"TEKRAR_GIT", "KONTROL_EDILDI"} else 0
        connection.execute(
            """UPDATE saha_durumlari SET durum=?,kontrol_sayisi=?,son_islem=?
            WHERE gorev_id=?""",
            (status, int(count or 0) + increment, _now(), task_id),
        )

        if source == "saha" and str(source_key).isdigit():
            site_id = int(source_key)
            active = 0 if status == "KONTROL_EDILDI" else 1
            connection.execute(
                "UPDATE santiyeler SET aktif=?,son_kontrol=? WHERE id=?",
                (active, _now(), site_id),
            )

        return {
            "gorev_id": task_id,
            "eski_durum": old_status,
            "yeni_durum": status,
            "kaynak": source,
        }


def status_counts(connection):
    ensure_state_schema(connection)
    rows = connection.execute(
        "SELECT durum,COUNT(*) FROM saha_durumlari GROUP BY durum"
    ).fetchall()
    return {str(status): int(count) for status, count in rows}
