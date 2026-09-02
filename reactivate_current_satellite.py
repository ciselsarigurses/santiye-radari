"""Güncel uydu analizinde yeniden anlam kazanan görevleri güvenli biçimde geri açar.

İki ayrı durum ele alınır:
1. Önceki bir analiz sürümünde otomatik olarak ``ALGORITMA_ELENDI`` yapılmış ve
   hiçbir saha işlemi görmemiş görev, güncel analizde tekrar desteklenirse açılır.
2. Sahada ``TARLA_BITKI`` olarak doğrulanıp kapatılmış bir nokta, *daha yeni bir
   Sentinel sahnesinde* yeniden gerçek bir değişim hotspot'u olarak görünürse tekrar
   ``KONTROLE_GIT`` yapılır. Böylece yeni temizlenmiş/sürülmüş bir arazinin birkaç
   gün sonra hafriyat veya temel başlangıcına dönüşmesi eski saha kararı yüzünden
   sessizce yutulmaz.

Aynı Sentinel sahnesinin tekrar raporlanması tarla görevini geri açmaz. Kullanıcının
``SANTIYE_KAZI``, ``YOL_ALTYAPI`` veya genel ``YANLIS_POZITIF`` kararları da bu
koruma tarafından otomatik değiştirilmez.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from daily_report import ISTANBUL, _report_hotspots, ensure_daily_schema
from field_outcome import ensure_outcome_schema
from field_state import INTERNAL_SUPERSEDED_STATUS, sync_satellite_tasks
from scanner import connect


def _now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _scene_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    return None


def _new_farmland_scene(saved_scene, current_scene, new_image):
    """Tarla sonucu ancak gerçekten daha yeni Sentinel kanıtıyla yeniden açılsın."""
    if not bool(new_image):
        return False
    saved = _scene_date(saved_scene)
    current = _scene_date(current_scene)
    return bool(saved and current and current > saved)


def _reactivate_farmland_followups(connection, current):
    """Yeni sahnede yeniden hareket eden, sahada tarla olarak doğrulanmış noktaları aç."""
    ensure_outcome_schema(connection)
    rows = connection.execute(
        """SELECT gorev_id,son_tarih FROM saha_sonuclari
        WHERE sonuc='TARLA_BITKI'"""
    ).fetchall()
    saved_scenes = {
        str(task_id): saved_scene
        for task_id, saved_scene in rows
        if str(task_id or "")
    }

    reopened = []
    for item in current:
        task_id = str(item.get("gorev_id") or "")
        if not task_id or task_id not in saved_scenes:
            continue
        if str(item.get("saha_durumu") or "") != "KONTROL_EDILDI":
            continue
        if not _new_farmland_scene(
            saved_scenes[task_id],
            item.get("son_tarih"),
            item.get("yeni_goruntu"),
        ):
            continue

        cursor = connection.execute(
            """UPDATE saha_durumlari SET durum='KONTROLE_GIT',son_islem=?
            WHERE gorev_id=? AND durum='KONTROL_EDILDI'""",
            (_now_utc(), task_id),
        )
        if cursor.rowcount:
            reopened.append(task_id)
    return reopened


def reactivate_current(report_date=None):
    """Güncel analizde yeniden desteklenen güvenli görevleri geri aç."""
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

        reactivated.extend(_reactivate_farmland_followups(connection, current))

    return reactivated


def _self_check():
    assert not _new_farmland_scene("29.08.2026", "29.08.2026", True)
    assert not _new_farmland_scene("29.08.2026", "01.09.2026", False)
    assert _new_farmland_scene("29.08.2026", "01.09.2026", True)
    assert _new_farmland_scene("2026-08-29", "2026-09-01", True)
    assert not _new_farmland_scene(None, "01.09.2026", True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    _self_check()
    if args.check_only:
        print(
            "Tarla takip koruması öz testi başarılı: aynı sahne kapalı kalıyor, "
            "yalnız daha yeni Sentinel sahnesindeki yeniden hareket görevi açıyor."
        )
        return

    task_ids = reactivate_current()
    if task_ids:
        print(
            "Güncel uydu analizinde yeniden kontrol edilmesi gereken güvenli görevler "
            "geri açıldı: " + ", ".join(task_ids)
        )
    else:
        print("Geri açılması gereken güncel uydu görevi yok.")


if __name__ == "__main__":
    main()
