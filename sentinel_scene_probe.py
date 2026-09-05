"""Sentinel sahnesi yayımlandığında tam radar taramasını gecikmeden tetiklemek için hafif yoklama.

Bu modül görüntü bantlarını indirmez ve saha alarmı üretmez. Yalnız Earth Search
STAC metadata'sından, üretim bölgelerinin tamamını örten en yeni karşılaştırılabilir
Sentinel öğesini bulur ve SQLite'ta son işlenen öğeyle karşılaştırır. Yeni öğe varsa
GitHub Actions workflow'u mevcut tam günlük taramayı workflow_dispatch ile çağırabilir.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import satellite


DB_PATH = Path(__file__).with_name("santiye.db")
ISTANBUL = ZoneInfo("Europe/Istanbul")
REGIONS = ("cesme", "uzunkuyu")


def _stored_latest_item(connection, region_key):
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gunluk_uydu_raporlari' LIMIT 1"
    ).fetchone()
    if not table:
        return None, None

    row = connection.execute(
        """SELECT son_item,rapor_tarihi
        FROM gunluk_uydu_raporlari
        WHERE bolge=? AND son_item IS NOT NULL AND TRIM(son_item)<>''
        ORDER BY rapor_tarihi DESC,id DESC LIMIT 1""",
        (region_key,),
    ).fetchone()
    if not row:
        return None, None
    return row[0], row[1]


def probe(connection, pair_provider=satellite.sentinel_pair):
    rows = []
    for region_key in REGIONS:
        stored_item, stored_date = _stored_latest_item(connection, region_key)
        try:
            _older, latest = pair_provider(region_key)
            live_item = latest.get("id")
            live_datetime = str(latest.get("properties", {}).get("datetime") or "")
            if not live_item:
                raise ValueError("Sentinel öğe kimliği boş")
            is_new = live_item != stored_item
            rows.append(
                {
                    "bolge": region_key,
                    "durum": "ok",
                    "kayitli_item": stored_item,
                    "kayitli_rapor_tarihi": stored_date,
                    "canli_item": live_item,
                    "canli_datetime": live_datetime,
                    "yeni_sahne": is_new,
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "bolge": region_key,
                    "durum": "hata",
                    "kayitli_item": stored_item,
                    "kayitli_rapor_tarihi": stored_date,
                    "canli_item": None,
                    "canli_datetime": None,
                    "yeni_sahne": False,
                    "hata": f"{type(exc).__name__}: {exc}",
                }
            )

    successful = [row for row in rows if row["durum"] == "ok"]
    return {
        "kontrol_zamani": datetime.now(ISTANBUL).isoformat(timespec="seconds"),
        "yeni_sahne": any(row["yeni_sahne"] for row in successful),
        "probe_ok": bool(successful),
        "bolgeler": rows,
        "not": (
            "Bu yalnız metadata yoklamasıdır; alarm/görev üretmez. Yeni sahne saptanırsa "
            "mevcut tam tarama workflow'u çalıştırılmalıdır."
        ),
    }


def _write_github_output(path, payload):
    if not path:
        return
    target = Path(path)
    changed_regions = [
        row["bolge"]
        for row in payload["bolgeler"]
        if row.get("durum") == "ok" and row.get("yeni_sahne")
    ]
    with target.open("a", encoding="utf-8") as handle:
        handle.write(f"new_scene={'true' if payload['yeni_sahne'] else 'false'}\n")
        handle.write(f"probe_ok={'true' if payload['probe_ok'] else 'false'}\n")
        handle.write(f"changed_regions={','.join(changed_regions)}\n")


def _self_check():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            """CREATE TABLE gunluk_uydu_raporlari (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rapor_tarihi TEXT,bolge TEXT,son_item TEXT)"""
        )
        connection.executemany(
            "INSERT INTO gunluk_uydu_raporlari (rapor_tarihi,bolge,son_item) VALUES(?,?,?)",
            [
                ("2026-09-03", "cesme", "CESME_OLD"),
                ("2026-09-03", "uzunkuyu", "UZUN_OLD"),
            ],
        )

        def unchanged(region_key):
            item_id = "CESME_OLD" if region_key == "cesme" else "UZUN_OLD"
            latest = {"id": item_id, "properties": {"datetime": "2026-09-03T09:00:00Z"}}
            return ({"id": "OLDER"}, latest)

        stable = probe(connection, unchanged)
        assert stable["probe_ok"] is True
        assert stable["yeni_sahne"] is False

        def one_changed(region_key):
            item_id = "CESME_NEW" if region_key == "cesme" else "UZUN_OLD"
            latest = {"id": item_id, "properties": {"datetime": "2026-09-05T09:00:00Z"}}
            return ({"id": "OLDER"}, latest)

        changed = probe(connection, one_changed)
        assert changed["yeni_sahne"] is True
        changed_rows = [row for row in changed["bolgeler"] if row["yeni_sahne"]]
        assert [row["bolge"] for row in changed_rows] == ["cesme"]

        def one_error(region_key):
            if region_key == "cesme":
                raise RuntimeError("gecici stac hatasi")
            latest = {"id": "UZUN_OLD", "properties": {"datetime": "2026-09-03T09:00:00Z"}}
            return ({"id": "OLDER"}, latest)

        partial = probe(connection, one_error)
        assert partial["probe_ok"] is True
        assert partial["yeni_sahne"] is False
        assert partial["bolgeler"][0]["durum"] == "hata"
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-output", default="")
    parser.add_argument("--self-check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if args.self_check_only:
        print("Sentinel hızlı sahne yoklama öz testi başarılı.")
        return 0

    with sqlite3.connect(DB_PATH) as connection:
        payload = probe(connection)
    _write_github_output(args.github_output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["probe_ok"]:
        print("Hiçbir üretim bölgesinde Sentinel metadata yoklaması tamamlanamadı.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
