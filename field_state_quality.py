"""Uydu saha görevlerinin yakın ama ayrı şantiyeleri tek göreve ezmediğini doğrular."""

from __future__ import annotations

import sqlite3

from field_state import (
    COMPLETED_REUSE_METERS,
    SATELLITE_MATCH_METERS,
    _distance_m,
    ensure_state_schema,
    satellite_task_id,
    sync_satellite_tasks,
)

SOURCE = "Çeşme merkez · Alaçatı · Ilıca"


def _item(latitude, longitude):
    return {
        "mahalle": "Musalla",
        "enlem": latitude,
        "boylam": longitude,
        "bolge": SOURCE,
        "alan_m2": 300,
        "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
    }


def check_close_distinct_hotspots_stay_distinct():
    connection = sqlite3.connect(":memory:")
    ensure_state_schema(connection)

    first = _item(38.30011, 26.30011)
    second = _item(38.30039, 26.30011)
    assert satellite_task_id(first) == satellite_task_id(second), (
        "Test verisi eski ~100 m görev hücresinde çakışmıyor."
    )
    distance = _distance_m(
        first["enlem"], first["boylam"], second["enlem"], second["boylam"]
    )
    assert 25 < distance < SATELLITE_MATCH_METERS

    initial = sync_satellite_tasks(connection, [first, second], "2026-08-29")
    assert len({item["gorev_id"] for item in initial}) == 2, (
        "25-80 m aralığındaki iki ayrı Sentinel hotspot'u ilk oluşumda tek göreve birleşti."
    )

    first_id = initial[0]["gorev_id"]
    connection.execute(
        "UPDATE saha_durumlari SET durum='TEKRAR_GIT' WHERE gorev_id=?",
        (first_id,),
    )

    # Sıralama değişse bile saha kararı fiziksel olarak en yakın eski göreve bağlı
    # kalmalı; liste sırası iki yakın şantiyenin kimliklerini birbirine geçirmemeli.
    current = sync_satellite_tasks(connection, [second, first], "2026-08-30")
    by_latitude = {round(item["enlem"], 5): item for item in current}
    assert len({item["gorev_id"] for item in current}) == 2
    assert by_latitude[round(first["enlem"], 5)]["gorev_id"] == first_id
    assert by_latitude[round(first["enlem"], 5)]["saha_durumu"] == "TEKRAR_GIT", (
        "Yakın hotspot sırası değişince mevcut saha kararı diğer fiziksel noktaya taşındı."
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM saha_durumlari WHERE kaynak='uydu'"
    ).fetchone()[0] == 2
    connection.close()


def check_single_centroid_drift_keeps_task():
    connection = sqlite3.connect(":memory:")
    ensure_state_schema(connection)

    first = _item(38.31010, 26.30049)
    shifted = _item(38.31010, 26.30060)
    assert satellite_task_id(first) != satellite_task_id(shifted), (
        "Test verisi eski kimlik hücresi sınırını geçmiyor."
    )
    distance = _distance_m(
        first["enlem"], first["boylam"], shifted["enlem"], shifted["boylam"]
    )
    assert distance < SATELLITE_MATCH_METERS

    original = sync_satellite_tasks(connection, [first], "2026-08-29")[0]
    moved = sync_satellite_tasks(connection, [shifted], "2026-08-30")[0]
    assert moved["gorev_id"] == original["gorev_id"], (
        "Tek bir şantiyenin normal Sentinel centroid kayması yeni görev üretti."
    )
    connection.close()


def check_completed_task_does_not_hide_neighbor():
    connection = sqlite3.connect(":memory:")
    ensure_state_schema(connection)

    checked = _item(38.32010, 26.30010)
    neighbor = _item(38.32040, 26.30010)
    distance = _distance_m(
        checked["enlem"], checked["boylam"], neighbor["enlem"], neighbor["boylam"]
    )
    assert COMPLETED_REUSE_METERS < distance < SATELLITE_MATCH_METERS

    completed = sync_satellite_tasks(connection, [checked], "2026-08-29")[0]
    completed_id = completed["gorev_id"]
    connection.execute(
        "UPDATE saha_durumlari SET durum='KONTROL_EDILDI',kontrol_sayisi=1 "
        "WHERE gorev_id=?",
        (completed_id,),
    )

    current = sync_satellite_tasks(connection, [neighbor], "2026-08-30")[0]
    assert current["gorev_id"] != completed_id, (
        "Kontrol edilmiş uydu görevi 25-80 m uzaktaki yeni hotspot'u yuttu."
    )
    assert current["saha_durumu"] == "KONTROLE_GIT", (
        "Kontrol edilmiş saha yanındaki yeni hotspot saha listesine yeni görev olarak girmedi."
    )
    old_row = connection.execute(
        "SELECT enlem,boylam,durum FROM saha_durumlari WHERE gorev_id=?",
        (completed_id,),
    ).fetchone()
    assert round(float(old_row[0]), 5) == round(checked["enlem"], 5)
    assert round(float(old_row[1]), 5) == round(checked["boylam"], 5)
    assert old_row[2] == "KONTROL_EDILDI", (
        "Yeni komşu hotspot eski tamamlanmış görevin konumunu veya kararını değiştirdi."
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM saha_durumlari WHERE kaynak='uydu'"
    ).fetchone()[0] == 2
    connection.close()


def check_completed_task_keeps_tiny_centroid_jitter():
    connection = sqlite3.connect(":memory:")
    ensure_state_schema(connection)

    checked = _item(38.33010, 26.30010)
    tiny_shift = _item(38.33020, 26.30010)
    distance = _distance_m(
        checked["enlem"], checked["boylam"], tiny_shift["enlem"], tiny_shift["boylam"]
    )
    assert distance < COMPLETED_REUSE_METERS

    completed = sync_satellite_tasks(connection, [checked], "2026-08-29")[0]
    completed_id = completed["gorev_id"]
    connection.execute(
        "UPDATE saha_durumlari SET durum='KONTROL_EDILDI',kontrol_sayisi=1 "
        "WHERE gorev_id=?",
        (completed_id,),
    )

    current = sync_satellite_tasks(connection, [tiny_shift], "2026-08-30")[0]
    assert current["gorev_id"] == completed_id, (
        "25 m altındaki normal centroid oynaması tamamlanmış aynı saha için yeni görev üretti."
    )
    assert current["saha_durumu"] == "KONTROL_EDILDI"
    assert connection.execute(
        "SELECT COUNT(*) FROM saha_durumlari WHERE kaynak='uydu'"
    ).fetchone()[0] == 1
    connection.close()


def main():
    check_close_distinct_hotspots_stay_distinct()
    check_single_centroid_drift_keeps_task()
    check_completed_task_does_not_hide_neighbor()
    check_completed_task_keeps_tiny_centroid_jitter()
    print(
        "Saha görev ayrıştırma kalite kontrolü başarılı: 25-80 m yakın iki ayrı "
        "hotspot tek göreve ezilmiyor; açık görevlerde normal centroid kayması "
        "korunuyor; kontrol edilmiş görevler 25 m dışındaki yeni komşu hotspot'u "
        "gizlemiyor."
    )


if __name__ == "__main__":
    main()
