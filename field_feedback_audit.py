"""Saha geri bildirimini güvenli kalibrasyon metriklerine dönüştürür.

Bu modül üretim alarm eşiklerini değiştirmez. Amaç, normal Sentinel saha görevleri
ve alarm-dışı kuru-zemin kalibrasyon kontrollerinden gelen doğrulanmış etiketleri
aynı yerde ölçmek; örnek sayısı yetersizken birkaç saha sonucuna aşırı uyum sağlayıp
filtreleri bozmayı engellemektir.

Yalnız ``saha_durumlari.kaynak='uydu'`` olan normal görevler Sentinel üretim
kalibrasyonuna girer. İnternet/belediye gibi başka kaynakların saha sonuçları ayrı
sayılır ve uydu doğruluk oranını kirletmez. Otomatik eşik değişikliği her durumda
kapalıdır; yeterli örnek yalnız manuel algoritma incelemesinin anlamlı olduğunu
belirtir.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict

from calibration_outcome import ensure_calibration_schema
from field_outcome import ALLOWED_OUTCOMES, ensure_outcome_schema
from scanner import connect


POSITIVE_OUTCOME = "SANTIYE_KAZI"
MIN_TOTAL_FOR_REVIEW = 12
MIN_CLASS_FOR_REVIEW = 4


def _size_bucket(area_m2):
    try:
        area = float(area_m2)
    except (TypeError, ValueError):
        return "bilinmiyor"
    if area < 250:
        return "250_alti"
    if area <= 800:
        return "250_800"
    if area <= 2_000:
        return "800_2000"
    if area <= 10_000:
        return "2000_10000"
    return "10000_ustu"


def _rate(positive, total):
    if not total:
        return None
    return round(positive / total, 4)


def _group_summary(rows, key_index, outcome_index=0):
    grouped = defaultdict(lambda: {"toplam": 0, "gercek_santiye": 0})
    for row in rows:
        key = str(row[key_index] or "bilinmiyor")
        grouped[key]["toplam"] += 1
        if str(row[outcome_index] or "") == POSITIVE_OUTCOME:
            grouped[key]["gercek_santiye"] += 1
    return {
        key: {
            **value,
            "gercek_santiye_orani": _rate(value["gercek_santiye"], value["toplam"]),
        }
        for key, value in sorted(grouped.items())
    }


def _read_production_rows(connection):
    # Eski DB'lerde saha_durumlari bulunmasa bile şema kurulmuş olmalı; normal
    # uygulama bu tabloyu zaten field_state üzerinden yaratır. Test/boş DB için
    # minimal uyumlu tabloyu burada kuruyoruz.
    connection.execute(
        """CREATE TABLE IF NOT EXISTS saha_durumlari (
        gorev_id TEXT PRIMARY KEY,
        kaynak TEXT,
        kaynak_kimlik TEXT,
        mahalle TEXT,
        enlem REAL,
        boylam REAL,
        durum TEXT,
        kontrol_sayisi INTEGER,
        ilk_gorulme TEXT,
        son_gorulme TEXT,
        son_islem TEXT)"""
    )
    rows = connection.execute(
        """SELECT s.sonuc,s.alan_m2,s.boyut_sinifi,s.uydu_onceligi,
        s.uydu_kanit_yasi_gun,s.tarihsel_esleme_mesafe_m,s.geometri_kaynagi,
        COALESCE(d.kaynak,'') AS kaynak
        FROM saha_sonuclari s
        LEFT JOIN saha_durumlari d ON d.gorev_id=s.gorev_id
        ORDER BY s.kayit_zamani"""
    ).fetchall()
    return rows


def _read_calibration_rows(connection):
    return connection.execute(
        """SELECT sonuc,alan_m2,zaman_serisi_ani_baslangic_orani,
        yerellik_orani,yerellik_yaygin_cevre_degisim_riski
        FROM kalibrasyon_sonuclari ORDER BY kayit_zamani"""
    ).fetchall()


def feedback_audit_summary(connection=None):
    owns_connection = connection is None
    connection = connection or connect()
    try:
        ensure_outcome_schema(connection)
        ensure_calibration_schema(connection)
        production_rows = _read_production_rows(connection)
        calibration_rows = _read_calibration_rows(connection)

        satellite_rows = [row for row in production_rows if str(row[7]) == "uydu"]
        other_rows = [row for row in production_rows if str(row[7]) != "uydu"]

        production_counts = Counter(str(row[0] or "") for row in satellite_rows)
        calibration_counts = Counter(str(row[0] or "") for row in calibration_rows)
        production_total = len(satellite_rows)
        production_positive = production_counts.get(POSITIVE_OUTCOME, 0)
        production_negative = production_total - production_positive
        calibration_total = len(calibration_rows)
        calibration_positive = calibration_counts.get(POSITIVE_OUTCOME, 0)
        calibration_negative = calibration_total - calibration_positive

        size_rows = [
            (row[0], _size_bucket(row[1]))
            for row in satellite_rows
        ]
        calibration_size_rows = [
            (row[0], _size_bucket(row[1]))
            for row in calibration_rows
        ]

        # İnce ayar için her iki sınıftan da örnek gerekir. Bu sınır istatistiksel
        # güven aralığı iddiası değildir; erken aşamada birkaç etikete aşırı uyumu
        # engelleyen muhafazakâr operasyonel kilittir.
        review_ready = (
            production_total >= MIN_TOTAL_FOR_REVIEW
            and production_positive >= MIN_CLASS_FOR_REVIEW
            and production_negative >= MIN_CLASS_FOR_REVIEW
        )
        if review_ready:
            status = "manuel_inceleme_icin_yeterli"
            reason = (
                "Sentinel üretim etiketlerinde iki sınıf da asgari örnek sayısına ulaştı; "
                "eşikler yine otomatik değiştirilmez, yalnız manuel karşılaştırma yapılabilir."
            )
        else:
            status = "etiket_yetersiz"
            reason = (
                f"Sentinel üretim kalibrasyonu için en az {MIN_TOTAL_FOR_REVIEW} toplam, "
                f"en az {MIN_CLASS_FOR_REVIEW} gerçek şantiye ve {MIN_CLASS_FOR_REVIEW} "
                "şantiye-dışı etiket beklenir; mevcut örnekle eşik oynamak aşırı uyum riski taşır."
            )

        return {
            "durum": status,
            "otomatik_esik_degistirme": False,
            "manuel_inceleme_hazir": review_ready,
            "neden": reason,
            "sentinel_uretim": {
                "toplam": production_total,
                "gercek_santiye": production_positive,
                "santiye_disi": production_negative,
                "gercek_santiye_orani": _rate(production_positive, production_total),
                "sonuclar": {key: production_counts.get(key, 0) for key in sorted(ALLOWED_OUTCOMES)},
                "boyut_bandi": _group_summary(size_rows, 1),
                "uydu_onceligi": _group_summary(satellite_rows, 3),
                "boyut_sinifi": _group_summary(satellite_rows, 2),
            },
            "alarm_disi_kalibrasyon": {
                "toplam": calibration_total,
                "gercek_santiye": calibration_positive,
                "santiye_disi": calibration_negative,
                "gercek_santiye_orani": _rate(calibration_positive, calibration_total),
                "sonuclar": {key: calibration_counts.get(key, 0) for key in sorted(ALLOWED_OUTCOMES)},
                "boyut_bandi": _group_summary(calibration_size_rows, 1),
                "temporal_ozellikli": sum(row[2] is not None for row in calibration_rows),
                "yerellik_ozellikli": sum(row[3] is not None for row in calibration_rows),
                "yaygin_cevre_riski_etiketli": sum(row[4] is not None for row in calibration_rows),
            },
            "uydu_disi_saha_sonucu": len(other_rows),
            "inceleme_esikleri": {
                "minimum_toplam": MIN_TOTAL_FOR_REVIEW,
                "minimum_sinif_basi": MIN_CLASS_FOR_REVIEW,
            },
        }
    finally:
        if owns_connection:
            connection.close()


def _insert_field(connection, task_id, outcome, area, size_class, priority, source="uydu"):
    connection.execute(
        """INSERT INTO saha_durumlari
        (gorev_id,kaynak,durum) VALUES(?,?,'KONTROL_EDILDI')""",
        (task_id, source),
    )
    connection.execute(
        """INSERT INTO saha_sonuclari
        (gorev_id,sonuc,kayit_zamani,alan_m2,boyut_sinifi,uydu_onceligi)
        VALUES(?,?,?, ?,?,?)""",
        (task_id, outcome, "2026-09-02 07:00 UTC", area, size_class, priority),
    )


def _self_check():
    connection = sqlite3.connect(":memory:")
    try:
        ensure_outcome_schema(connection)
        ensure_calibration_schema(connection)
        # Önce yetersiz örnek: uydu dışı sonuç uydu doğruluk paydasına girmemeli.
        _insert_field(connection, "U1", "SANTIYE_KAZI", 300, "KUCUK", "YUKSEK")
        _insert_field(connection, "S1", "YANLIS_POZITIF", 900, "STANDART", "NORMAL", source="internet")
        summary = feedback_audit_summary(connection)
        assert summary["sentinel_uretim"]["toplam"] == 1, summary
        assert summary["uydu_disi_saha_sonucu"] == 1, summary
        assert not summary["manuel_inceleme_hazir"], summary
        assert not summary["otomatik_esik_degistirme"], summary

        # 12 uydu etiketi, iki sınıfta da en az dört örnek: yalnız manuel inceleme
        # kilidi açılır, otomatik eşik değişikliği yine kapalı kalır.
        for index in range(2, 13):
            positive = index <= 6
            _insert_field(
                connection,
                f"U{index}",
                "SANTIYE_KAZI" if positive else "YANLIS_POZITIF",
                300 if index % 2 else 1200,
                "KUCUK" if index % 2 else "STANDART",
                "YUKSEK" if positive else "NORMAL",
            )
        summary = feedback_audit_summary(connection)
        assert summary["sentinel_uretim"]["toplam"] == 12, summary
        assert summary["sentinel_uretim"]["gercek_santiye"] == 6, summary
        assert summary["sentinel_uretim"]["santiye_disi"] == 6, summary
        assert summary["manuel_inceleme_hazir"], summary
        assert not summary["otomatik_esik_degistirme"], summary
        assert summary["sentinel_uretim"]["boyut_bandi"]["250_800"]["toplam"] > 0
        assert summary["sentinel_uretim"]["boyut_bandi"]["800_2000"]["toplam"] > 0
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    if args.check_only:
        _self_check()
        print("Saha geri bildirim kalibrasyon kilidi öz testi başarılı.")
        return
    print(json.dumps(feedback_audit_summary(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
