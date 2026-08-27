"""Günlük raporu aktif radar adaylarıyla tutarlı hale getirir.

Aynı gün içinde birden fazla workflow çalıştığında son taramanın ``yeni`` sayısı
sıfıra dönebilir. Günlük rapor ise o gün ilk kez görülen adayları göstermelidir.
Ayrıca sonradan gürültü/eskimiş diye pasife alınan adayların saha raporunda
kalmaması gerekir. Bu adım günlük özet, veritabanı ve açık rapor dosyalarını
aynı aktif veri kümesinden yeniden üretir.
"""

from __future__ import annotations

import json
from datetime import datetime

from daily_report import ISTANBUL, _report_hotspots, _write_public_report
from scanner import connect


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
        satellite = _satellite_summary(connection, report_date)
        summary = (
            f"İnternet: {daily_new} yeni aktif bulgu, {updated} güncellendi. "
            f"Instagram: {instagram} yeni indekslenmiş sonuç. "
            f"Belediye: {municipality} yeni açık sonuç."
        )
        if satellite:
            summary += " " + " · ".join(satellite)

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
        hotspots = _report_hotspots(connection, report_date)

    _write_public_report(report_date, created, summary, hotspots, details)
    return {
        "date": report_date,
        "active_daily_findings": daily_new,
        "summary": summary,
    }


if __name__ == "__main__":
    result = normalize_daily_report()
    if result:
        print(
            f"Rapor kalite kontrolü tamamlandı ({result['date']}): "
            f"{result['active_daily_findings']} aktif günlük bulgu"
        )
    else:
        print("Kalite kontrolü için günlük rapor bulunamadı.")
