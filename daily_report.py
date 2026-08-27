"""İnternet, belediye, Instagram ve ücretsiz uydu günlük raporunu üretir."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from satellite import REGIONS, analyze_sentinel_change, sentinel_pair
from scanner import connect, ensure_schema


REPORT_REGIONS = ("cesme", "uzunkuyu")
ISTANBUL = ZoneInfo("Europe/Istanbul")


def _columns(connection, table):
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_columns(connection, table, definitions):
    existing = _columns(connection, table)
    for name, definition in definitions.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def ensure_daily_schema():
    ensure_schema()
    with connect() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS gunluk_raporlar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rapor_tarihi TEXT UNIQUE,
            olusturma TEXT,
            internet_bulgu INTEGER DEFAULT 0,
            internet_yeni INTEGER DEFAULT 0,
            internet_guncellenen INTEGER DEFAULT 0,
            instagram_yeni INTEGER DEFAULT 0,
            belediye_yeni INTEGER DEFAULT 0,
            internet_detay_json TEXT,
            ozet TEXT)"""
        )
        _add_columns(
            connection,
            "gunluk_raporlar",
            {
                "instagram_yeni": "INTEGER DEFAULT 0",
                "belediye_yeni": "INTEGER DEFAULT 0",
                "internet_detay_json": "TEXT",
                "ozet": "TEXT",
            },
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS gunluk_uydu_raporlari (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rapor_tarihi TEXT,
            bolge TEXT,
            bolge_adi TEXT,
            onceki_tarih TEXT,
            son_tarih TEXT,
            onceki_item TEXT,
            son_item TEXT,
            yeni_goruntu INTEGER DEFAULT 0,
            degisim_km2 REAL,
            degisim_yuzde REAL,
            bulut_yuzde REAL,
            hareket_json TEXT,
            hata TEXT,
            UNIQUE(rapor_tarihi, bolge))"""
        )


def _internet_snapshot(connection, report_date):
    scan = connection.execute(
        """SELECT bulunan,yeni,guncellenen FROM tarama_gecmisi
        ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    found, new, updated = scan if scan else (0, 0, 0)
    rows = connection.execute(
        """SELECT proje,bolge,sinyal,kaynak_url,kaynak_tipi,skor,durum
        FROM internet_adaylari
        WHERE ilk_gorulme LIKE ?
        ORDER BY skor DESC LIMIT 20""",
        (f"{report_date}%",),
    ).fetchall()
    details = [
        {
            "proje": row[0], "bolge": row[1], "sinyal": row[2],
            "kaynak_url": row[3], "kaynak_tipi": row[4],
            "skor": row[5], "durum": row[6],
        }
        for row in rows
    ]
    instagram = sum(
        "instagram" in str(item.get("kaynak_tipi", "")).casefold()
        for item in details
    )
    municipality = sum(
        "belediye" in str(item.get("kaynak_tipi", "")).casefold()
        for item in details
    )
    return int(found), int(new), int(updated), instagram, municipality, details


def _last_satellite_item(connection, region_key, report_date):
    row = connection.execute(
        """SELECT son_item FROM gunluk_uydu_raporlari
        WHERE bolge=? AND rapor_tarihi<? AND son_item IS NOT NULL
        ORDER BY rapor_tarihi DESC LIMIT 1""",
        (region_key, report_date),
    ).fetchone()
    return row[0] if row else None


def _existing_today(connection, region_key, report_date, latest_item):
    row = connection.execute(
        """SELECT yeni_goruntu,hareket_json,hata FROM gunluk_uydu_raporlari
        WHERE bolge=? AND rapor_tarihi=? LIMIT 1""",
        (region_key, report_date),
    ).fetchone()
    item_row = connection.execute(
        """SELECT son_item FROM gunluk_uydu_raporlari
        WHERE bolge=? AND rapor_tarihi=? LIMIT 1""",
        (region_key, report_date),
    ).fetchone()
    if not row or not item_row or item_row[0] != latest_item:
        return None
    return row


def _store_satellite(connection, values):
    connection.execute(
        """INSERT INTO gunluk_uydu_raporlari
        (rapor_tarihi,bolge,bolge_adi,onceki_tarih,son_tarih,onceki_item,
        son_item,yeni_goruntu,degisim_km2,degisim_yuzde,bulut_yuzde,
        hareket_json,hata)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(rapor_tarihi,bolge) DO UPDATE SET
        bolge_adi=excluded.bolge_adi,onceki_tarih=excluded.onceki_tarih,
        son_tarih=excluded.son_tarih,onceki_item=excluded.onceki_item,
        son_item=excluded.son_item,yeni_goruntu=excluded.yeni_goruntu,
        degisim_km2=excluded.degisim_km2,
        degisim_yuzde=excluded.degisim_yuzde,
        bulut_yuzde=excluded.bulut_yuzde,
        hareket_json=excluded.hareket_json,hata=excluded.hata""",
        values,
    )


def build_daily_report():
    ensure_daily_schema()
    now = datetime.now(ISTANBUL)
    report_date = now.strftime("%Y-%m-%d")
    created = now.strftime("%Y-%m-%d %H:%M %Z")
    satellite_summaries = []

    with connect() as connection:
        found, new, updated, instagram, municipality, details = _internet_snapshot(
            connection, report_date
        )

        for region_key in REPORT_REGIONS:
            region_name = REGIONS[region_key]["label"]
            try:
                older, latest = sentinel_pair(region_key)
                latest_item = latest["id"]
                existing = _existing_today(
                    connection, region_key, report_date, latest_item
                )
                if existing:
                    existing_movement = json.loads(existing[1] or "[]")
                    if existing[2]:
                        satellite_summaries.append(
                            f"{region_name}: uydu kontrolü tamamlanamadı"
                        )
                    elif existing[0]:
                        satellite_summaries.append(
                            f"{region_name}: {len(existing_movement)} hareket bölgesi adayı"
                        )
                    else:
                        satellite_summaries.append(
                            f"{region_name}: yeni uydu görüntüsü yok"
                        )
                    continue
                previous_latest = _last_satellite_item(
                    connection, region_key, report_date
                )
                is_new = previous_latest != latest_item
                if is_new:
                    result = analyze_sentinel_change(region_key, pair=(older, latest))
                    movement = result["hotspots"]
                    _store_satellite(
                        connection,
                        (
                            report_date, region_key, region_name,
                            result["older_date"], result["latest_date"],
                            older["id"], latest_item, 1,
                            result["changed_km2"], result["changed_percent"],
                            result["latest_cloud"],
                            json.dumps(movement, ensure_ascii=False), None,
                        ),
                    )
                    satellite_summaries.append(
                        f"{region_name}: {len(movement)} hareket bölgesi adayı"
                    )
                else:
                    latest_date = datetime.fromisoformat(
                        latest["properties"]["datetime"].replace("Z", "+00:00")
                    ).strftime("%d.%m.%Y")
                    older_date = datetime.fromisoformat(
                        older["properties"]["datetime"].replace("Z", "+00:00")
                    ).strftime("%d.%m.%Y")
                    _store_satellite(
                        connection,
                        (
                            report_date, region_key, region_name,
                            older_date, latest_date, older["id"], latest_item,
                            0, None, None,
                            float(latest["properties"].get("eo:cloud_cover", 0)),
                            "[]", None,
                        ),
                    )
                    satellite_summaries.append(
                        f"{region_name}: yeni uydu görüntüsü yok"
                    )
            except Exception as exc:
                _store_satellite(
                    connection,
                    (
                        report_date, region_key, region_name,
                        None, None, None, None, 0, None, None, None,
                        "[]", f"{type(exc).__name__}: {exc}",
                    ),
                )
                satellite_summaries.append(
                    f"{region_name}: uydu kontrolü tamamlanamadı"
                )

        summary = (
            f"İnternet: {new} yeni, {updated} güncellendi. "
            f"Instagram: {instagram} yeni indekslenmiş sonuç. "
            f"Belediye: {municipality} yeni açık sonuç. "
            + " · ".join(satellite_summaries)
        )
        connection.execute(
            """INSERT INTO gunluk_raporlar
            (rapor_tarihi,olusturma,internet_bulgu,internet_yeni,
            internet_guncellenen,instagram_yeni,belediye_yeni,
            internet_detay_json,ozet)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(rapor_tarihi) DO UPDATE SET
            olusturma=excluded.olusturma,internet_bulgu=excluded.internet_bulgu,
            internet_yeni=excluded.internet_yeni,
            internet_guncellenen=excluded.internet_guncellenen,
            instagram_yeni=excluded.instagram_yeni,
            belediye_yeni=excluded.belediye_yeni,
            internet_detay_json=excluded.internet_detay_json,
            ozet=excluded.ozet""",
            (
                report_date, created, found, new, updated, instagram,
                municipality, json.dumps(details, ensure_ascii=False), summary,
            ),
        )
    return {"date": report_date, "summary": summary}


if __name__ == "__main__":
    report = build_daily_report()
    print(f"Günlük rapor hazır ({report['date']}): {report['summary']}")
