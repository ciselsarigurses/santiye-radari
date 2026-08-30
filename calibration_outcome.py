"""Alarm olmayan kuru-zemin kalibrasyon kontrollerini ayrı ve kalıcı saklar.

Bu kayıtlar ``saha_durumlari`` veya normal ``saha_sonuclari`` tablolarına girmez;
böylece kalibrasyon ziyareti üretim alarmı/görevi sayısını ve saha doğruluk
istatistiğini şişirmez. Kimlik, aynı uydu bölgesi + Sentinel tarih çifti için
sabittir; aynı görüntü çifti tekrar raporlansa bile ekip aynı kalibrasyonu yeniden
yapmaya yönlendirilmez.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from field_outcome import ALLOWED_OUTCOMES, OUTCOME_LABELS
from scanner import connect


CALIBRATION_ID_PATTERN = re.compile(r"^K[A-F0-9]{10}$")
CALIBRATION_KIND = "KURU_ZEMIN"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def calibration_id(item):
    """Bir bölge ve Sentinel tarih çifti için kararlı kalibrasyon kimliği üret."""
    item = item if isinstance(item, dict) else {}
    region = str(item.get("bolge_anahtari") or item.get("bolge") or "").strip()
    start = str(item.get("onceki_tarih") or "").strip()
    end = str(item.get("son_tarih") or "").strip()

    # Günlük rota şu anda bölge başına tek kalibrasyon noktası seçiyor. Tarih çifti
    # varsa koordinatı kimliğe katmamak, aynı sahne tekrar üretildiğinde örnek sırası
    # değişse bile aynı bölgenin ikinci kez kalibrasyona çıkmasını engeller.
    if region and start and end:
        key = f"{CALIBRATION_KIND}|{region}|{start}|{end}"
    else:
        latitude = str(item.get("enlem") or "").strip()
        longitude = str(item.get("boylam") or "").strip()
        key = f"{CALIBRATION_KIND}|{region}|{start}|{end}|{latitude}|{longitude}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10].upper()
    return f"K{digest}"


def ensure_calibration_schema(connection=None):
    owns_connection = connection is None
    connection = connection or connect()
    try:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS kalibrasyon_sonuclari (
            kalibrasyon_id TEXT PRIMARY KEY,
            tur TEXT NOT NULL,
            sonuc TEXT NOT NULL,
            bolge TEXT,
            mahalle TEXT,
            enlem REAL,
            boylam REAL,
            alan_m2 REAL,
            onceki_tarih TEXT,
            son_tarih TEXT,
            kayit_zamani TEXT NOT NULL)"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_kalibrasyon_sonuc "
            "ON kalibrasyon_sonuclari(sonuc)"
        )
        if owns_connection:
            connection.commit()
    finally:
        if owns_connection:
            connection.close()


def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def save_calibration_outcome(calibration_key, outcome, item=None):
    calibration_key = str(calibration_key or "").strip().upper()
    outcome = str(outcome or "").strip().upper()
    if not CALIBRATION_ID_PATTERN.fullmatch(calibration_key):
        raise ValueError("Geçersiz kalibrasyon kimliği.")
    if outcome not in ALLOWED_OUTCOMES:
        raise ValueError("Bilinmeyen kalibrasyon saha sonucu.")

    item = item if isinstance(item, dict) else {}
    with connect() as connection:
        ensure_calibration_schema(connection)
        connection.execute(
            """INSERT INTO kalibrasyon_sonuclari
            (kalibrasyon_id,tur,sonuc,bolge,mahalle,enlem,boylam,alan_m2,
             onceki_tarih,son_tarih,kayit_zamani)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(kalibrasyon_id) DO UPDATE SET
            sonuc=excluded.sonuc,bolge=excluded.bolge,mahalle=excluded.mahalle,
            enlem=excluded.enlem,boylam=excluded.boylam,alan_m2=excluded.alan_m2,
            onceki_tarih=excluded.onceki_tarih,son_tarih=excluded.son_tarih,
            kayit_zamani=excluded.kayit_zamani""",
            (
                calibration_key,
                CALIBRATION_KIND,
                outcome,
                str(item.get("bolge") or "") or None,
                str(item.get("mahalle") or "") or None,
                _float_or_none(item.get("enlem")),
                _float_or_none(item.get("boylam")),
                _float_or_none(item.get("alan_m2")),
                str(item.get("onceki_tarih") or "") or None,
                str(item.get("son_tarih") or "") or None,
                _now(),
            ),
        )
    return outcome


def calibration_outcome_map():
    with connect() as connection:
        ensure_calibration_schema(connection)
        rows = connection.execute(
            """SELECT kalibrasyon_id,sonuc,bolge,mahalle,enlem,boylam,alan_m2,
            onceki_tarih,son_tarih,kayit_zamani
            FROM kalibrasyon_sonuclari ORDER BY kayit_zamani DESC"""
        ).fetchall()
    return {
        str(row[0]): {
            "sonuc": str(row[1]),
            "etiket": OUTCOME_LABELS.get(str(row[1]), str(row[1])),
            "bolge": row[2],
            "mahalle": row[3],
            "enlem": row[4],
            "boylam": row[5],
            "alan_m2": row[6],
            "onceki_tarih": row[7],
            "son_tarih": row[8],
            "kayit_zamani": row[9],
        }
        for row in rows
    }


def _self_check():
    sample = {
        "bolge_anahtari": "cesme",
        "bolge": "Çeşme merkez · Alaçatı · Ilıca",
        "onceki_tarih": "26.08.2026",
        "son_tarih": "29.08.2026",
        "enlem": 38.355516,
        "boylam": 26.300190,
    }
    first = calibration_id(sample)
    moved_example = {**sample, "enlem": 38.355700, "boylam": 26.300400}
    assert first == calibration_id(moved_example), (
        "Aynı bölge+tarih çiftinde örnek koordinatı değişse de kalibrasyon kimliği sabit kalmalı."
    )
    assert CALIBRATION_ID_PATTERN.fullmatch(first)
    assert first != calibration_id({**sample, "son_tarih": "01.09.2026"})


if __name__ == "__main__":
    _self_check()
    print("Kalibrasyon geri bildirim kimliği kalite kontrolü başarılı.")
