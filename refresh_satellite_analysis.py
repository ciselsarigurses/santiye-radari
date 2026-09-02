"""Uydu analiz algoritması değiştiğinde mevcut görüntüyü güvenle bir kez yeniden işler."""

from __future__ import annotations

import json
from datetime import datetime

from daily_report import ISTANBUL, REPORT_REGIONS, ensure_daily_schema
from satellite import REGIONS, analyze_sentinel_change, sentinel_pair
from scanner import connect


# Bu değer yalnızca uydu değişim mantığı anlamlı biçimde değiştiğinde artırılır.
# v15: SCL su sınıfının çevresine yaklaşık 30 m kıyı tamponu eklenir. Böylece
# dalga, ıslak kaya, kıyı platformu ve kara/deniz karma pikselleri toprak hareketi
# olarak saha görevi üretmez. Mahalle merkezinden 3 km uzaktaki yaklaşık etiketler
# de kesin mahalle adı yerine doğrulanmamış mevki olarak işaretlenir.
ANALYSIS_VERSION = "native-10m-full-envelope-wgs84-cesme-admin-buffer-cap24-same-orbit-uri-scl2-shadow-coast30-place3km-v15"


def _ensure_version_table(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS uydu_analiz_surumu (
        bolge TEXT PRIMARY KEY,
        surum TEXT NOT NULL,
        guncelleme TEXT NOT NULL)"""
    )


def _previous_latest(connection, region_key, report_date):
    row = connection.execute(
        """SELECT son_item FROM gunluk_uydu_raporlari
        WHERE bolge=? AND rapor_tarihi<? AND son_item IS NOT NULL
        ORDER BY rapor_tarihi DESC LIMIT 1""",
        (region_key, report_date),
    ).fetchone()
    return row[0] if row else None


def _existing_new_image_flag(connection, region_key, report_date, latest_item):
    row = connection.execute(
        """SELECT son_item,yeni_goruntu FROM gunluk_uydu_raporlari
        WHERE bolge=? AND rapor_tarihi=? LIMIT 1""",
        (region_key, report_date),
    ).fetchone()
    if row and row[0] == latest_item:
        return int(bool(row[1]))
    return None


def _store_result(connection, report_date, region_key, result, older, latest, new_image):
    region_name = REGIONS[region_key]["label"]
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
        degisim_km2=excluded.degisim_km2,degisim_yuzde=excluded.degisim_yuzde,
        bulut_yuzde=excluded.bulut_yuzde,
        hareket_json=excluded.hareket_json,hata=excluded.hata""",
        (
            report_date,
            region_key,
            region_name,
            result["older_date"],
            result["latest_date"],
            older["id"],
            latest["id"],
            new_image,
            result["changed_km2"],
            result["changed_percent"],
            result["latest_cloud"],
            json.dumps(result["hotspots"], ensure_ascii=False),
            None,
        ),
    )


def _reset_temporal_state_if_present(connection, region_key):
    """Ana analiz değişince her iki tamamlayıcı gölge/bulut cache'ini de yeniler."""
    for table_name in ("uydu_zaman_serisi", "uydu_son_bulut_boslugu"):
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table_name,),
        ).fetchone()
        if exists:
            connection.execute(f"DELETE FROM {table_name} WHERE bolge=?", (region_key,))


def refresh_if_needed():
    """Yeni analiz sürümünü mevcut son görüntüye uygular; aynı sürümü tekrarlamaz."""
    ensure_daily_schema()
    now = datetime.now(ISTANBUL)
    report_date = now.strftime("%Y-%m-%d")
    refreshed = []
    skipped = []
    errors = []

    with connect() as connection:
        _ensure_version_table(connection)
        for region_key in REPORT_REGIONS:
            current = connection.execute(
                "SELECT surum FROM uydu_analiz_surumu WHERE bolge=?",
                (region_key,),
            ).fetchone()
            if current and current[0] == ANALYSIS_VERSION:
                skipped.append(region_key)
                continue

            try:
                older, latest = sentinel_pair(region_key)
                latest_item = latest["id"]
                existing_flag = _existing_new_image_flag(
                    connection, region_key, report_date, latest_item
                )
                if existing_flag is None:
                    previous_latest = _previous_latest(connection, region_key, report_date)
                    new_image = int(previous_latest != latest_item)
                else:
                    new_image = existing_flag

                result = analyze_sentinel_change(region_key, pair=(older, latest))
                _store_result(
                    connection,
                    report_date,
                    region_key,
                    result,
                    older,
                    latest,
                    new_image,
                )
                connection.execute(
                    """INSERT INTO uydu_analiz_surumu (bolge,surum,guncelleme)
                    VALUES(?,?,?)
                    ON CONFLICT(bolge) DO UPDATE SET
                    surum=excluded.surum,guncelleme=excluded.guncelleme""",
                    (region_key, ANALYSIS_VERSION, now.isoformat()),
                )
                _reset_temporal_state_if_present(connection, region_key)
                refreshed.append(
                    (
                        region_key,
                        len(result["hotspots"]),
                        result.get("older_relative_orbit"),
                        result.get("latest_relative_orbit"),
                        result.get("older_date"),
                        result.get("latest_date"),
                    )
                )
            except Exception as exc:
                errors.append(f"{region_key}: {type(exc).__name__}: {exc}")

    return refreshed, skipped, errors


if __name__ == "__main__":
    refreshed, skipped, errors = refresh_if_needed()
    if refreshed:
        text = ", ".join(
            (
                f"{region}={count} aday "
                f"({older_date}→{latest_date}, göreli yörünge "
                f"{older_orbit if older_orbit is not None else '?'}→"
                f"{latest_orbit if latest_orbit is not None else '?'})"
            )
            for (
                region,
                count,
                older_orbit,
                latest_orbit,
                older_date,
                latest_date,
            ) in refreshed
        )
        print(f"Uydu analiz sürümü yenilendi ({ANALYSIS_VERSION}): {text}")
    else:
        print(f"Uydu analiz sürümü güncel ({ANALYSIS_VERSION}); yeniden işleme gerekmedi.")
    if skipped:
        print("Atlanan güncel bölgeler: " + ", ".join(skipped))
    if errors:
        print(
            "Yeniden işleme hataları (sonraki çalışmada tekrar denenecek): "
            + " | ".join(errors)
        )
