"""Uydu analiz algoritması değiştiğinde mevcut görüntüyü güvenle bir kez yeniden işler."""

from __future__ import annotations

import json
import sqlite3
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

LEGACY_UZUNKUYU_LABEL = "Uzunkuyu · Germiyan · Ildır"
CURRENT_UZUNKUYU_LABEL = "Uzunkuyu · Germiyan · Ildır · Gülbahçe"
INTERNAL_SUPERSEDED_STATUS = "ALGORITMA_ELENDI"


def _ensure_version_table(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS uydu_analiz_surumu (
        bolge TEXT PRIMARY KEY,
        surum TEXT NOT NULL,
        guncelleme TEXT NOT NULL)"""
    )


def _normalize_legacy_satellite_source_labels(connection):
    """Gülbahçe etiket geçişinin aynı Sentinel sahasını yeni görev gibi çoğaltmasını önler.

    ``saha_durumlari.kaynak_kimlik`` geçmişte insan-okur bölge etiketiyle tutuldu.
    Uzunkuyu üretim etiketi Gülbahçe eklendiğinde aynı koordinat yeni kaynak kimliği
    sanılabildi ve yeni Sentinel görüntüsü olmadan ikinci bir görev açılabildi.

    Eski etiketi güncel etikete taşır. Etiket geçişi sırasında oluşmuş, koordinatı
    altı ondalıkta birebir aynı ve hiç saha işlemi görmemiş açık görevler varsa en
    eski görev korunur; yalnız daha yeni otomasyon kopyaları ALGORITMA_ELENDI yapılır.
    TEKRAR_GIT, KONTROL_EDILDI veya kullanıcı işlemi görmüş kayıtlar değiştirilmez.
    """
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='saha_durumlari' LIMIT 1"
    ).fetchone()
    if not table_exists:
        return 0, 0

    migrated = connection.execute(
        """UPDATE saha_durumlari SET kaynak_kimlik=?
        WHERE kaynak='uydu' AND kaynak_kimlik=?""",
        (CURRENT_UZUNKUYU_LABEL, LEGACY_UZUNKUYU_LABEL),
    ).rowcount

    rows = connection.execute(
        """SELECT gorev_id,mahalle,enlem,boylam,ilk_gorulme,son_gorulme,
        son_islem,COALESCE(kontrol_sayisi,0)
        FROM saha_durumlari
        WHERE kaynak='uydu' AND kaynak_kimlik=? AND durum='KONTROLE_GIT'
        AND enlem IS NOT NULL AND boylam IS NOT NULL""",
        (CURRENT_UZUNKUYU_LABEL,),
    ).fetchall()

    groups = {}
    for row in rows:
        task_id, neighborhood, latitude, longitude = row[:4]
        try:
            key = (
                str(neighborhood or "").casefold().strip(),
                round(float(latitude), 6),
                round(float(longitude), 6),
            )
        except (TypeError, ValueError):
            continue
        groups.setdefault(key, []).append(row)

    superseded = 0
    for duplicates in groups.values():
        if len(duplicates) < 2:
            continue
        # En eski görev kimliği saha geçmişinin taşıyıcısıdır. Tarih eşitse DB
        # görevi deterministik kalsın diye görev kimliği ikinci anahtardır.
        ordered = sorted(
            duplicates,
            key=lambda row: (
                str(row[4] or "9999-99-99"),
                str(row[0] or ""),
            ),
        )
        keep_id = str(ordered[0][0] or "")
        for row in ordered[1:]:
            task_id = str(row[0] or "")
            control_count = int(row[7] or 0)
            if not task_id or task_id == keep_id or control_count:
                continue
            cursor = connection.execute(
                """UPDATE saha_durumlari SET durum=?,son_islem=?
                WHERE gorev_id=? AND durum='KONTROLE_GIT'
                AND COALESCE(kontrol_sayisi,0)=0""",
                (
                    INTERNAL_SUPERSEDED_STATUS,
                    datetime.now(ISTANBUL).isoformat(),
                    task_id,
                ),
            )
            superseded += int(cursor.rowcount or 0)

    return int(migrated or 0), superseded


def _source_label_compatibility_self_check():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            """CREATE TABLE saha_durumlari (
            gorev_id TEXT PRIMARY KEY,kaynak TEXT,kaynak_kimlik TEXT,mahalle TEXT,
            enlem REAL,boylam REAL,durum TEXT,kontrol_sayisi INTEGER,
            ilk_gorulme TEXT,son_gorulme TEXT,son_islem TEXT)"""
        )
        connection.executemany(
            """INSERT INTO saha_durumlari VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    "UOLD", "uydu", LEGACY_UZUNKUYU_LABEL, "Germiyan",
                    38.331275, 26.484866, "KONTROLE_GIT", 0,
                    "2026-08-29", "2026-09-02", "2026-08-29",
                ),
                (
                    "UNEW", "uydu", CURRENT_UZUNKUYU_LABEL, "Germiyan",
                    38.331275, 26.484866, "KONTROLE_GIT", 0,
                    "2026-09-03", "2026-09-03", "2026-09-03",
                ),
                (
                    "UREPEAT", "uydu", LEGACY_UZUNKUYU_LABEL, "Germiyan",
                    38.331275, 26.484866, "TEKRAR_GIT", 1,
                    "2026-08-30", "2026-09-03", "2026-09-03",
                ),
            ],
        )
        migrated, superseded = _normalize_legacy_satellite_source_labels(connection)
        assert migrated == 2, migrated
        assert superseded == 1, superseded
        assert connection.execute(
            "SELECT COUNT(*) FROM saha_durumlari WHERE kaynak_kimlik=?",
            (LEGACY_UZUNKUYU_LABEL,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT durum FROM saha_durumlari WHERE gorev_id='UOLD'"
        ).fetchone()[0] == "KONTROLE_GIT"
        assert connection.execute(
            "SELECT durum FROM saha_durumlari WHERE gorev_id='UNEW'"
        ).fetchone()[0] == INTERNAL_SUPERSEDED_STATUS
        assert connection.execute(
            "SELECT durum FROM saha_durumlari WHERE gorev_id='UREPEAT'"
        ).fetchone()[0] == "TEKRAR_GIT"
    finally:
        connection.close()


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
    _source_label_compatibility_self_check()
    now = datetime.now(ISTANBUL)
    report_date = now.strftime("%Y-%m-%d")
    refreshed = []
    skipped = []
    errors = []

    with connect() as connection:
        migrated, superseded = _normalize_legacy_satellite_source_labels(connection)
        if migrated or superseded:
            print(
                "Uydu görev bölge etiketi uyumluluğu: "
                f"{migrated} eski Uzunkuyu etiketi güncellendi; "
                f"{superseded} birebir koordinatlı dokunulmamış kopya pasifleştirildi."
            )
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