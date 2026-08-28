"""İnternet, belediye, Instagram ve ücretsiz uydu günlük raporunu üretir."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from satellite import REGIONS, analyze_sentinel_change, sentinel_pair
from scanner import connect, ensure_schema


REPORT_REGIONS = ("cesme", "uzunkuyu")
ISTANBUL = ZoneInfo("Europe/Istanbul")
FIELD_REPORT_MD = Path(__file__).with_name("SAHA_RAPORU.md")
LATEST_REPORT_JSON = Path(__file__).with_name("latest_report.json")


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
        WHERE aktif=1 AND ilk_gorulme LIKE ?
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


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _field_priority(area_m2):
    """Alan büyüklüğüne göre saha ziyaret sırası; güven skoru değildir."""
    if area_m2 >= 5000:
        return "YÜKSEK"
    if area_m2 >= 2000:
        return "ORTA"
    return "NORMAL"


def _maps_route(latitude, longitude):
    return (
        "https://www.google.com/maps/dir/?api=1&destination="
        f"{latitude:.6f},{longitude:.6f}"
    )


def _report_hotspots(connection, report_date):
    rows = connection.execute(
        """SELECT bolge,bolge_adi,onceki_tarih,son_tarih,yeni_goruntu,
        hareket_json,hata
        FROM gunluk_uydu_raporlari
        WHERE rapor_tarihi=? ORDER BY bolge""",
        (report_date,),
    ).fetchall()
    results = []
    for row in rows:
        try:
            movement = json.loads(row[5] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            movement = []
        if not isinstance(movement, list):
            continue
        for item in movement:
            if not isinstance(item, dict):
                continue
            latitude = _number(item.get("enlem"), None)
            longitude = _number(item.get("boylam"), None)
            if latitude is None or longitude is None:
                continue
            area_m2 = max(_number(item.get("alan_m2"), 0), 0)
            results.append(
                {
                    "oncelik": _field_priority(area_m2),
                    "mahalle": str(item.get("mahalle") or "Yakın mevki bilinmiyor"),
                    "enlem": round(latitude, 6),
                    "boylam": round(longitude, 6),
                    "alan_m2": round(area_m2),
                    "sinyal": str(item.get("sinyal") or "Yüzey değişimi adayı"),
                    "bolge": str(row[1] or row[0] or "-"),
                    "onceki_tarih": row[2],
                    "son_tarih": row[3],
                    "yeni_goruntu": bool(row[4]),
                    "harita": _maps_route(latitude, longitude),
                    "konum_notu": (
                        "Uydu değişim kümesinin yaklaşık merkezidir; kesin adres veya "
                        "parsel değildir."
                    ),
                }
            )
    priority_order = {"YÜKSEK": 0, "ORTA": 1, "NORMAL": 2}
    return sorted(
        results,
        key=lambda item: (
            priority_order.get(item["oncelik"], 9),
            -item["alan_m2"],
        ),
    )


def _md_text(value, fallback="-"):
    text = str(value or fallback).replace("\n", " ").strip() or fallback
    return text.replace("[", "(").replace("]", ")")


def _active_priority_opportunities(report_date, limit=10):
    """Taze kalan KIRMIZI internet fırsatlarını günlük raporda görünür tutar."""
    with connect() as connection:
        rows = connection.execute(
            """SELECT proje,firma,bolge,sinyal,kaynak_url,kaynak_tipi,skor,durum,
            ilk_gorulme,son_gorulme
            FROM internet_adaylari
            WHERE aktif=1 AND durum='KIRMIZI' AND COALESCE(skor,0)>=8
            ORDER BY skor DESC, son_gorulme DESC, id DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
    return [
        {
            "proje": row[0],
            "firma": row[1],
            "bolge": row[2],
            "sinyal": row[3],
            "kaynak_url": row[4],
            "kaynak_tipi": row[5],
            "skor": int(row[6] or 0),
            "durum": row[7],
            "ilk_gorulme": row[8],
            "son_gorulme": row[9],
            "yeni": str(row[8] or "")[:10] == report_date,
        }
        for row in rows
    ]


def _write_public_report(report_date, created, summary, hotspots, details):
    opportunities = _active_priority_opportunities(report_date)
    payload = {
        "rapor_tarihi": report_date,
        "olusturma": created,
        "ozet": summary,
        "saha_adaylari": hotspots,
        "yeni_internet_bulgulari": details,
        "oncelikli_internet_firsatlari": opportunities,
        "uyari": (
            "Uydu koordinatları yaklaşık değişim merkezidir. Kesin adres/ada-parsel "
            "olarak kullanılmamalı; saha kontrolüyle doğrulanmalıdır."
        ),
    }
    LATEST_REPORT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Şantiye Radarı — Günlük Saha Raporu",
        "",
        f"**Rapor tarihi:** {report_date}",
        f"**Hazırlanma:** {created}",
        "",
        f"**Özet:** {summary}",
        "",
        "> **Konum kuralı:** Uydu noktası değişim kümesinin yaklaşık merkezidir. "
        "Kesin adres veya ada/parsel doğrulanmadıkça yazılmaz.",
        "",
        "## Bugün sahada kontrol edilecek uydu adayları",
        "",
    ]
    if hotspots:
        for index, item in enumerate(hotspots, start=1):
            area_text = f"{int(item['alan_m2']):,}".replace(",", ".")
            interval = (
                f"{_md_text(item.get('onceki_tarih'))} → "
                f"{_md_text(item.get('son_tarih'))}"
            )
            lines.extend(
                [
                    f"### {index}. {item['oncelik']} — {_md_text(item['mahalle'])}",
                    f"- **Yaklaşık konum:** {_md_text(item['bolge'])} / "
                    f"{_md_text(item['mahalle'])}",
                    f"- **Koordinat:** `{item['enlem']}, {item['boylam']}`",
                    f"- **Değişim alanı:** yaklaşık {area_text} m²",
                    f"- **Görüntü aralığı:** {interval}",
                    f"- **Sinyal:** {_md_text(item['sinyal'])}",
                    f"- **Rota:** [Google Maps'te aç]({item['harita']})",
                    "- **Saha talimatı:** Konumu yerinde kontrol et. Kazı, temel, "
                    "şantiye kurulumu veya aktif inşaat görülürse fotoğraf çek; "
                    "firma/tabela ve mümkünse doğru adres bilgisini kaydet.",
                    "",
                ]
            )
    else:
        lines.extend(
            [
                "Bugünkü raporda eşik üstünde yeni uydu hareket adayı yok.",
                "",
            ]
        )

    lines.extend(["## Bugünün yeni internet / sosyal medya bulguları", ""])
    if details:
        for item in sorted(
            details,
            key=lambda candidate: int(candidate.get("skor") or 0),
            reverse=True,
        ):
            title = _md_text(item.get("proje"), "Başlıksız bulgu")
            location = _md_text(item.get("bolge"))
            signal = _md_text(item.get("sinyal"))
            status = _md_text(item.get("durum"))
            score = int(item.get("skor") or 0)
            url = str(item.get("kaynak_url") or "").strip()
            if url.startswith(("http://", "https://")):
                title = f"[{title}]({url})"
            lines.append(
                f"- **{status} · {score} puan · {location}:** {title} — {signal}"
            )
    else:
        lines.append("Bugün ilk kez bulunan yeni internet sonucu yok.")

    lines.extend(["", "## Aktif güçlü internet fırsatları", ""])
    if opportunities:
        for item in opportunities:
            title = _md_text(item.get("proje"), "Başlıksız bulgu")
            location = _md_text(item.get("bolge"))
            signal = _md_text(item.get("sinyal"))
            score = int(item.get("skor") or 0)
            badge = "YENİ" if item.get("yeni") else "AKTİF"
            url = str(item.get("kaynak_url") or "").strip()
            if url.startswith(("http://", "https://")):
                title = f"[{title}]({url})"
            lines.append(
                f"- **{badge} · KIRMIZI · {score} puan · {location}:** "
                f"{title} — {signal}"
            )
    else:
        lines.append("Şu anda aktif KIRMIZI internet fırsatı yok.")

    lines.extend(
        [
            "",
            "---",
            "**Not:** YÜKSEK / ORTA / NORMAL sırası yalnızca uydu değişim alanının "
            "büyüklüğüne göre saha ziyaret önceliğidir; inşaat olduğuna dair güven "
            "skoru değildir. Yanlış pozitifler saha kontrolüyle elenir.",
            "",
        ]
    )
    FIELD_REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


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
        hotspots = _report_hotspots(connection, report_date)
        _write_public_report(report_date, created, summary, hotspots, details)
    return {
        "date": report_date,
        "summary": summary,
        "field_candidates": len(hotspots),
    }


if __name__ == "__main__":
    report = build_daily_report()
    print(
        f"Günlük rapor hazır ({report['date']}): {report['summary']} "
        f"· Saha adayı: {report['field_candidates']}"
    )
