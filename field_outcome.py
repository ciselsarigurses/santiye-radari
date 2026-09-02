"""Saha kontrollerinin yapılandırılmış sonucunu ayrı tabloda saklar.

Mevcut saha durum mekanizmasına dokunmadan, doğrulanmış saha geri bildirimlerini
gelecekte yanlış pozitifleri azaltmak için kullanılabilecek biçimde toplar.

Sentinel saha sonucu yalnız sınıf etiketi olarak bırakılmaz. Sonuç kaydedildiği anda
görevin raporda görünen alan/konum, Sentinel tarih çifti, ölçek ve öncelik bilgileri de
aynı satırda saklanır. Böylece saha geri bildirimi daha sonra küçük kazı, yol/altyapı
ve tarla yanlış pozitiflerini ölçmekte kullanılabilir. Bu modül kendi başına alarm,
eşik veya görev önceliği değiştirmez.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone

from scanner import connect


ALLOWED_OUTCOMES = {
    "SANTIYE_KAZI",
    "YOL_ALTYAPI",
    "TARLA_BITKI",
    "YANLIS_POZITIF",
}

OUTCOME_LABELS = {
    "SANTIYE_KAZI": "Gerçek şantiye / kazı / temel",
    "YOL_ALTYAPI": "Yol / altyapı çalışması",
    "TARLA_BITKI": "Tarla / bitki değişimi",
    "YANLIS_POZITIF": "Yanlış pozitif / başka neden",
}

FEATURE_COLUMNS = {
    "mahalle": "TEXT",
    "enlem": "REAL",
    "boylam": "REAL",
    "alan_m2": "REAL",
    "bolge": "TEXT",
    "onceki_tarih": "TEXT",
    "son_tarih": "TEXT",
    "ilk_gorulme": "TEXT",
    "oncelik": "TEXT",
    "uydu_onceligi": "TEXT",
    "boyut_sinifi": "TEXT",
    "uydu_kanit_yasi_gun": "INTEGER",
    "tarihsel_esleme_mesafe_m": "REAL",
    "geometri_kaynagi": "TEXT",
    "yeni_goruntu": "INTEGER",
    "sinyal": "TEXT",
    "oncelik_nedeni": "TEXT",
}


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _columns(connection, table):
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_columns(connection, table, definitions):
    existing = _columns(connection, table)
    for name, definition in definitions.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def ensure_outcome_schema(connection=None):
    owns_connection = connection is None
    connection = connection or connect()
    try:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS saha_sonuclari (
            gorev_id TEXT PRIMARY KEY,
            sonuc TEXT NOT NULL,
            kayit_zamani TEXT NOT NULL)"""
        )
        _add_columns(connection, "saha_sonuclari", FEATURE_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_saha_sonuc ON saha_sonuclari(sonuc)"
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


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text_or_none(value):
    text = str(value or "").strip()
    return text or None


def _bool_or_none(item, key):
    if key not in item or item.get(key) is None:
        return None
    return int(bool(item.get(key)))


def _feature_values(item):
    item = item if isinstance(item, dict) else {}
    return {
        "mahalle": _text_or_none(item.get("mahalle")),
        "enlem": _float_or_none(item.get("enlem")),
        "boylam": _float_or_none(item.get("boylam")),
        "alan_m2": _float_or_none(item.get("alan_m2")),
        "bolge": _text_or_none(item.get("bolge")),
        "onceki_tarih": _text_or_none(item.get("onceki_tarih")),
        "son_tarih": _text_or_none(item.get("son_tarih")),
        "ilk_gorulme": _text_or_none(item.get("ilk_gorulme")),
        "oncelik": _text_or_none(item.get("oncelik")),
        "uydu_onceligi": _text_or_none(item.get("uydu_onceligi")),
        "boyut_sinifi": _text_or_none(item.get("boyut_sinifi")),
        "uydu_kanit_yasi_gun": _int_or_none(item.get("uydu_kanit_yasi_gun")),
        "tarihsel_esleme_mesafe_m": _float_or_none(
            item.get("tarihsel_esleme_mesafe_m")
        ),
        "geometri_kaynagi": _text_or_none(item.get("geometri_kaynagi")),
        "yeni_goruntu": _bool_or_none(item, "yeni_goruntu"),
        "sinyal": _text_or_none(item.get("sinyal")),
        "oncelik_nedeni": _text_or_none(item.get("oncelik_nedeni")),
    }


def _upsert_outcome(connection, task_id, outcome, item=None, recorded_at=None):
    ensure_outcome_schema(connection)
    features = _feature_values(item)
    columns = ["gorev_id", "sonuc", "kayit_zamani", *FEATURE_COLUMNS]
    values = [
        task_id,
        outcome,
        recorded_at or _now(),
        *(features[name] for name in FEATURE_COLUMNS),
    ]
    placeholders = ",".join("?" for _ in columns)
    # Bir görev daha sonra yeniden sonuçlandırılırken güncel raporda artık
    # bulunamıyorsa, daha önce saklanan özellikleri NULL ile silme.
    updates = [
        "sonuc=excluded.sonuc",
        "kayit_zamani=excluded.kayit_zamani",
        *[
            f"{name}=COALESCE(excluded.{name},saha_sonuclari.{name})"
            for name in FEATURE_COLUMNS
        ],
    ]
    connection.execute(
        f"""INSERT INTO saha_sonuclari({','.join(columns)})
        VALUES({placeholders})
        ON CONFLICT(gorev_id) DO UPDATE SET {','.join(updates)}""",
        values,
    )


def save_outcome(task_id, outcome, item=None):
    task_id = str(task_id or "").strip().upper()
    outcome = str(outcome or "").strip().upper()
    if outcome not in ALLOWED_OUTCOMES:
        raise ValueError("Bilinmeyen saha kontrol sonucu.")

    with connect() as connection:
        _upsert_outcome(connection, task_id, outcome, item)
    return outcome


def clear_outcome(task_id):
    task_id = str(task_id or "").strip().upper()
    with connect() as connection:
        ensure_outcome_schema(connection)
        connection.execute("DELETE FROM saha_sonuclari WHERE gorev_id=?", (task_id,))


def outcome_map():
    with connect() as connection:
        ensure_outcome_schema(connection)
        rows = connection.execute(
            "SELECT gorev_id,sonuc,kayit_zamani FROM saha_sonuclari"
        ).fetchall()
    return {
        str(task_id): {
            "sonuc": str(outcome),
            "etiket": OUTCOME_LABELS.get(str(outcome), str(outcome)),
            "kayit_zamani": recorded_at,
        }
        for task_id, outcome, recorded_at in rows
    }


def _self_check():
    connection = sqlite3.connect(":memory:")
    try:
        ensure_outcome_schema(connection)
        assert set(FEATURE_COLUMNS).issubset(_columns(connection, "saha_sonuclari"))
        sample = {
            "mahalle": "Şifne",
            "enlem": 38.346018,
            "boylam": 26.383414,
            "alan_m2": 300,
            "bolge": "Çeşme merkez · Alaçatı · Ilıca",
            "onceki_tarih": "26.08.2026",
            "son_tarih": "29.08.2026",
            "ilk_gorulme": "2026-08-29",
            "oncelik": "GECİKEN",
            "uydu_onceligi": "ORTA",
            "boyut_sinifi": "KUCUK",
            "uydu_kanit_yasi_gun": 4,
            "tarihsel_esleme_mesafe_m": 8.9,
            "yeni_goruntu": False,
        }
        _upsert_outcome(
            connection,
            "UTEST",
            "SANTIYE_KAZI",
            sample,
            recorded_at="2026-09-02 06:00 UTC",
        )
        row = connection.execute(
            """SELECT sonuc,mahalle,alan_m2,boyut_sinifi,uydu_kanit_yasi_gun
            FROM saha_sonuclari WHERE gorev_id='UTEST'"""
        ).fetchone()
        assert row == ("SANTIYE_KAZI", "Şifne", 300.0, "KUCUK", 4), row

        # Güncel raporda artık bulunamayan bir tekrar kayıt, mevcut özellik
        # snapshot'ını silmemeli; yalnız sonuç/zaman güncellenmeli.
        _upsert_outcome(
            connection,
            "UTEST",
            "YOL_ALTYAPI",
            None,
            recorded_at="2026-09-02 07:00 UTC",
        )
        row = connection.execute(
            """SELECT sonuc,mahalle,alan_m2,boyut_sinifi
            FROM saha_sonuclari WHERE gorev_id='UTEST'"""
        ).fetchone()
        assert row == ("YOL_ALTYAPI", "Şifne", 300.0, "KUCUK"), row
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.check_only:
        parser.error("Bu modül doğrudan yalnız --check-only ile çalıştırılır.")
    _self_check()
    print(
        "Saha sonucu öz testi başarılı: Sentinel görev özellikleri sonuçla birlikte "
        "saklanıyor ve eksik tekrar kayıt eski snapshot'ı silmiyor."
    )


if __name__ == "__main__":
    main()
