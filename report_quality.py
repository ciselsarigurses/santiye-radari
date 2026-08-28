"""Günlük raporu aktif radar adayları ve saha kararlarıyla tutarlı tutar."""

from __future__ import annotations

import json
from datetime import datetime

from daily_report import ISTANBUL, _maps_route, _report_hotspots, _write_public_report
from field_outcome import ensure_outcome_schema
from field_state import (
    reconcile_satellite_duplicates,
    status_counts,
    sync_satellite_tasks,
    sync_site_tasks,
)
from scanner import connect


OVERDUE_FIELD_DAYS = 2
FIELD_OUTCOME_KEYS = (
    "SANTIYE_KAZI",
    "YOL_ALTYAPI",
    "TARLA_BITKI",
    "YANLIS_POZITIF",
)


def _active_daily_details(connection, report_date):
    rows = connection.execute(
        """SELECT proje,bolge,sinyal,kaynak_url,kaynak_tipi,skor,durum
        FROM internet_adaylari
        WHERE aktif=1 AND ilk_gorulme LIKE ?
        ORDER BY skor DESC, id DESC LIMIT 50""",
        (f"{report_date}%",),
    ).fetchall()
    return [
        {
            "proje": row[0],
            "bolge": row[1],
            "sinyal": row[2],
            "kaynak_url": row[3],
            "kaynak_tipi": row[4],
            "skor": row[5],
            "durum": row[6],
        }
        for row in rows
    ]


def _latest_scan_error_count(connection):
    """Son internet taramasında kaç kaynak/arama hatası olduğunu döndürür."""
    row = connection.execute(
        "SELECT hata FROM tarama_gecmisi ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row or not row[0]:
        return 0
    return sum(1 for line in str(row[0]).splitlines() if line.strip())


def _field_outcome_counts(connection):
    """Doğrulanmış saha sonuçlarını kategori bazında sayar."""
    ensure_outcome_schema(connection)
    rows = connection.execute(
        "SELECT sonuc,COUNT(*) FROM saha_sonuclari GROUP BY sonuc"
    ).fetchall()
    raw = {str(outcome): int(count) for outcome, count in rows}
    return {key: raw.get(key, 0) for key in FIELD_OUTCOME_KEYS}


def _satellite_summary(connection, report_date):
    rows = connection.execute(
        """SELECT bolge_adi,yeni_goruntu,hareket_json,hata
        FROM gunluk_uydu_raporlari
        WHERE rapor_tarihi=? ORDER BY bolge""",
        (report_date,),
    ).fetchall()
    summaries = []
    for region_name, has_new_image, movement_json, error in rows:
        name = region_name or "Uydu bölgesi"
        if error:
            summaries.append(f"{name}: uydu kontrolü tamamlanamadı")
            continue
        if not has_new_image:
            summaries.append(f"{name}: yeni uydu görüntüsü yok")
            continue
        try:
            movement = json.loads(movement_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            movement = []
        count = len(movement) if isinstance(movement, list) else 0
        summaries.append(f"{name}: {count} hareket bölgesi adayı")
    return summaries


def _waiting_days(first_seen, report_date):
    """Bir saha görevinin kaç takvim gündür açık olduğunu güvenli biçimde hesaplar."""
    try:
        first_date = datetime.fromisoformat(str(first_seen or "")[:10]).date()
        current_date = datetime.fromisoformat(str(report_date)[:10]).date()
    except (TypeError, ValueError):
        return 0
    return max((current_date - first_date).days, 0)


def _task_age_map(connection, task_ids, report_date):
    """Aktif görevlerin ilk görülme ve bekleme süresini tek sorguda getirir."""
    task_ids = sorted({str(task_id) for task_id in task_ids if str(task_id)})
    if not task_ids:
        return {}
    placeholders = ",".join("?" for _ in task_ids)
    rows = connection.execute(
        f"""SELECT gorev_id,ilk_gorulme FROM saha_durumlari
        WHERE gorev_id IN ({placeholders})""",
        task_ids,
    ).fetchall()
    return {
        str(task_id): {
            "ilk_gorulme": first_seen,
            "bekleme_gun": _waiting_days(first_seen, report_date),
        }
        for task_id, first_seen in rows
    }


def _persisted_open_tasks(connection, known_task_ids, report_date):
    """Yeni görüntüde görünmese de açık uydu saha kararlarını günlük listede tut."""
    rows = connection.execute(
        """SELECT d.gorev_id,d.kaynak,d.kaynak_kimlik,d.mahalle,d.enlem,d.boylam,
        d.kontrol_sayisi,d.son_islem,s.adres,s.ada,s.parsel,s.firma,s.proje,d.durum,
        d.ilk_gorulme
        FROM saha_durumlari d
        LEFT JOIN santiyeler s
          ON d.kaynak='saha' AND CAST(s.id AS TEXT)=d.kaynak_kimlik
        WHERE (d.kaynak='uydu' AND d.durum IN ('KONTROLE_GIT','TEKRAR_GIT'))
           OR (d.kaynak='saha' AND d.durum='TEKRAR_GIT')
        ORDER BY CASE d.durum WHEN 'TEKRAR_GIT' THEN 0 ELSE 1 END,
        d.son_islem DESC, d.id DESC"""
    ).fetchall()
    pending = []
    for row in rows:
        task_id = str(row[0] or "")
        if not task_id or task_id in known_task_ids:
            continue
        latitude, longitude = row[4], row[5]
        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except (TypeError, ValueError):
            latitude = longitude = None
        route = (
            _maps_route(latitude, longitude)
            if latitude is not None and longitude is not None
            else None
        )
        source = str(row[1] or "")
        source_key = str(row[2] or "")
        status = str(row[13] or "KONTROLE_GIT")
        first_seen = row[14]
        waiting_days = _waiting_days(first_seen, report_date)
        is_repeat = status == "TEKRAR_GIT"
        is_overdue = status == "KONTROLE_GIT" and waiting_days >= OVERDUE_FIELD_DAYS
        pending.append(
            {
                "oncelik": "TEKRAR" if is_repeat else ("GECİKEN" if is_overdue else "BEKLEYEN"),
                "mahalle": str(row[3] or "Konum araştırılıyor"),
                "enlem": round(latitude, 6) if latitude is not None else None,
                "boylam": round(longitude, 6) if longitude is not None else None,
                "alan_m2": 0,
                "sinyal": (
                    "Önceki saha kararı: bir daha git bak"
                    if is_repeat
                    else (
                        f"{waiting_days} gündür saha kontrolü bekliyor"
                        if is_overdue
                        else "Önceki uydu saha görevi: kontrol bekliyor"
                    )
                ),
                "bolge": source_key if source == "uydu" else "Saha listesi",
                "onceki_tarih": None,
                "son_tarih": None,
                "yeni_goruntu": False,
                "harita": route,
                "konum_notu": (
                    "Yeni uydu sinyali olmasa da saha ekibi tekrar kontrol istediği "
                    "için aktif takipte tutuluyor."
                    if is_repeat
                    else "Yeni uydu sinyali olmasa da saha kontrolü tamamlanmadığı "
                    "için aktif listede tutuluyor."
                ),
                "gorev_id": task_id,
                "saha_durumu": status,
                "takip_gorevi": is_repeat,
                "gecikmis": is_overdue,
                "ilk_gorulme": first_seen,
                "bekleme_gun": waiting_days,
                "kontrol_sayisi": int(row[6] or 0),
                "son_islem": row[7],
                "adres": row[8],
                "ada": row[9],
                "parsel": row[10],
                "firma": row[11],
                "proje": row[12],
            }
        )
    return pending


def _active_hotspots(connection, report_date):
    raw = _report_hotspots(connection, report_date)
    sync_site_tasks(connection, report_date)
    decorated = sync_satellite_tasks(connection, raw, report_date)
    reconcile_satellite_duplicates(connection, decorated, report_date)

    age_map = _task_age_map(
        connection,
        (item.get("gorev_id") for item in decorated),
        report_date,
    )
    active = []
    known_task_ids = set()
    for item in decorated:
        task_id = str(item.get("gorev_id") or "")
        if task_id:
            known_task_ids.add(task_id)
        status = str(item.get("saha_durumu") or "KONTROLE_GIT")
        if status == "KONTROL_EDILDI":
            continue
        item = dict(item)
        age = age_map.get(task_id, {})
        waiting_days = int(age.get("bekleme_gun") or 0)
        item["ilk_gorulme"] = age.get("ilk_gorulme")
        item["bekleme_gun"] = waiting_days
        item["gecikmis"] = False
        if status == "TEKRAR_GIT":
            item["oncelik"] = "TEKRAR"
            item["takip_gorevi"] = True
            item["sinyal"] = "Tekrar saha kontrolü · " + str(item.get("sinyal") or "")
        elif status == "KONTROLE_GIT" and waiting_days >= OVERDUE_FIELD_DAYS:
            item["uydu_onceligi"] = item.get("oncelik")
            item["oncelik"] = "GECİKEN"
            item["gecikmis"] = True
            item["sinyal"] = (
                f"{waiting_days} gündür saha kontrolü bekliyor · "
                + str(item.get("sinyal") or "")
            )
        active.append(item)

    active.extend(_persisted_open_tasks(connection, known_task_ids, report_date))

    priority = {
        "TEKRAR": 0,
        "GECİKEN": 1,
        "YÜKSEK": 2,
        "ORTA": 3,
        "BEKLEYEN": 4,
        "NORMAL": 5,
    }
    active.sort(
        key=lambda item: (
            priority.get(str(item.get("oncelik")), 9),
            -int(item.get("bekleme_gun") or 0),
            -float(item.get("alan_m2") or 0),
        )
    )
    return active


def normalize_daily_report(report_date=None):
    now = datetime.now(ISTANBUL)
    report_date = report_date or now.strftime("%Y-%m-%d")

    with connect() as connection:
        report = connection.execute(
            """SELECT olusturma,internet_bulgu,internet_guncellenen
            FROM gunluk_raporlar WHERE rapor_tarihi=?""",
            (report_date,),
        ).fetchone()
        if not report:
            return None

        created = report[0] or now.strftime("%Y-%m-%d %H:%M %Z")
        found = int(report[1] or 0)
        updated = int(report[2] or 0)
        details = _active_daily_details(connection, report_date)
        daily_new = len(details)
        instagram = sum(
            "instagram" in str(item.get("kaynak_tipi") or "").casefold()
            for item in details
        )
        municipality = sum(
            "belediye" in str(item.get("kaynak_tipi") or "").casefold()
            for item in details
        )
        scan_error_count = _latest_scan_error_count(connection)
        satellite = _satellite_summary(connection, report_date)
        hotspots = _active_hotspots(connection, report_date)
        counts = status_counts(connection)
        repeat_count = counts.get("TEKRAR_GIT", 0)
        overdue_count = sum(bool(item.get("gecikmis")) for item in hotspots)
        field_outcomes = _field_outcome_counts(connection)
        field_outcomes_total = sum(field_outcomes.values())

        summary = (
            f"İnternet: {daily_new} yeni aktif bulgu, {updated} güncellendi. "
            f"Instagram: {instagram} yeni indekslenmiş sonuç. "
            f"Belediye: {municipality} yeni açık sonuç."
        )
        if scan_error_count:
            summary += f" · Tarama uyarısı: {scan_error_count} kaynak/arama hatası"
        if satellite:
            summary += " " + " · ".join(satellite)
        summary += f" · Aktif saha görevi: {len(hotspots)}"
        if overdue_count:
            summary += f" · Geciken kontrol: {overdue_count}"
        if repeat_count:
            summary += f" · Tekrar gidilecek: {repeat_count}"
        if field_outcomes_total:
            summary += (
                f" · Saha sonucu: {field_outcomes_total} kontrol "
                f"({field_outcomes['SANTIYE_KAZI']} şantiye/kazı, "
                f"{field_outcomes['YOL_ALTYAPI']} yol/altyapı, "
                f"{field_outcomes['TARLA_BITKI']} tarla/bitki, "
                f"{field_outcomes['YANLIS_POZITIF']} yanlış pozitif)"
            )

        connection.execute(
            """UPDATE gunluk_raporlar SET
            internet_bulgu=?, internet_yeni=?, instagram_yeni=?, belediye_yeni=?,
            internet_detay_json=?, ozet=?
            WHERE rapor_tarihi=?""",
            (
                found,
                daily_new,
                instagram,
                municipality,
                json.dumps(details, ensure_ascii=False),
                summary,
                report_date,
            ),
        )

    _write_public_report(report_date, created, summary, hotspots, details)
    return {
        "date": report_date,
        "active_daily_findings": daily_new,
        "active_field_tasks": len(hotspots),
        "field_outcomes_total": field_outcomes_total,
        "field_outcomes": field_outcomes,
        "summary": summary,
    }


if __name__ == "__main__":
    result = normalize_daily_report()
    if result:
        print(
            f"Rapor kalite kontrolü tamamlandı ({result['date']}): "
            f"{result['active_daily_findings']} aktif günlük bulgu, "
            f"{result['active_field_tasks']} aktif saha görevi, "
            f"{result['field_outcomes_total']} saha sonucu"
        )
    else:
        print("Kalite kontrolü için günlük rapor bulunamadı.")
