"""Sentinel sahnesi yayımlandığında tam radar taramasını gecikmeden tetiklemek için hafif yoklama.

Bu modül görüntü bantlarını indirmez ve saha alarmı üretmez. Yalnız Earth Search
STAC metadata'sından, üretim bölgelerinin tamamını örten en yeni karşılaştırılabilir
Sentinel öğesini bulur ve SQLite'ta son işlenen öğeyle karşılaştırır. Yeni öğe varsa
GitHub Actions workflow'u mevcut tam günlük taramayı workflow_dispatch ile çağırabilir.

Saatlik metadata yoklamasında tek bir geçici ağ/5xx/429 hatası özellikle Uzunkuyu–
Gülbahçe tarafındaki yeni sahneyi bir sonraki saate erteleyebilir. Bu nedenle yalnız
geçici HTTP/ağ hataları bölge başına sınırlı sayıda yeniden denenir; kalıcı veri/
geometri hataları gereksiz STAC sorgusu üretmemek için tekrar edilmez.

Gülbahçe ayrı bir üretim Sentinel kutusu değildir; Uzunkuyu doğu kutusunun içindedir.
Yine de saatlik yoklamada sessiz kapsama regresyonu yaşanmaması için Gülbahçe'nin
2 km operasyon alanı + 150 m analiz bağlamı ayrıca doğrulanır ve çıktı içinde açıkça
gösterilir. Bu kontrol üçüncü bir STAC sorgusu yapmaz ve alarm/görev üretmez.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import satellite


DB_PATH = Path(__file__).with_name("santiye.db")
ISTANBUL = ZoneInfo("Europe/Istanbul")
STAC_REGIONS = ("cesme", "uzunkuyu")
GULBAHCE_PROBE_KEY = "gulbahce"
GULBAHCE_SOURCE_REGION = "uzunkuyu"
GULBAHCE_CENTER = (38.33278, 26.64556)
GULBAHCE_OPERATION_CONTEXT_RADIUS_M = 2_150
PROBE_ATTEMPTS = 3
PROBE_RETRY_SECONDS = 1.0


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


def _retryable_probe_error(exc):
    """Yalnız gerçekten geçici olabilecek STAC/ağ hatalarını yeniden dene."""
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return status == 429 or (isinstance(status, int) and status >= 500)
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def _probe_pair(
    pair_provider,
    region_key,
    attempts=PROBE_ATTEMPTS,
    retry_seconds=PROBE_RETRY_SECONDS,
    sleep_fn=time.sleep,
):
    """Bir bölgenin Sentinel çiftini geçici STAC hatalarında sınırlı yeniden dene."""
    attempts = max(int(attempts), 1)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return pair_provider(region_key), attempt
        except Exception as exc:
            last_error = exc
            if attempt >= attempts or not _retryable_probe_error(exc):
                raise
            delay = max(float(retry_seconds), 0.0) * attempt
            if delay:
                sleep_fn(delay)
    raise last_error


def _bbox_covers_radius(bbox, center, radius_m):
    """WGS84 kutusunun merkez çevresindeki yaklaşık metre tamponunu içerdiğini doğrula."""
    try:
        west, south, east, north = map(float, bbox)
        latitude, longitude = map(float, center)
        radius_m = max(float(radius_m), 0.0)
    except (TypeError, ValueError):
        return False

    latitude_margin = radius_m / 110_570
    longitude_scale = 111_320 * math.cos(math.radians(latitude))
    if longitude_scale <= 0:
        return False
    longitude_margin = radius_m / longitude_scale
    return (
        west <= longitude - longitude_margin
        and south <= latitude - latitude_margin
        and east >= longitude + longitude_margin
        and north >= latitude + latitude_margin
    )


def _gulbahce_coverage_row(rows):
    """Uzunkuyu yoklamasından Gülbahçe için açık ve test edilebilir kapsama kaydı üret."""
    source = next(
        (row for row in rows if row.get("bolge") == GULBAHCE_SOURCE_REGION),
        None,
    )
    region = satellite.REGIONS.get(GULBAHCE_SOURCE_REGION, {})
    bbox = region.get("bbox") if isinstance(region, dict) else None
    covered = _bbox_covers_radius(
        bbox,
        GULBAHCE_CENTER,
        GULBAHCE_OPERATION_CONTEXT_RADIUS_M,
    )

    row = {
        "bolge": GULBAHCE_PROBE_KEY,
        "kaynak_bolge": GULBAHCE_SOURCE_REGION,
        "metadata_sorgusu": False,
        "operasyon_merkezi": {
            "enlem": GULBAHCE_CENTER[0],
            "boylam": GULBAHCE_CENTER[1],
        },
        "operasyon_ve_analiz_yaricapi_m": GULBAHCE_OPERATION_CONTEXT_RADIUS_M,
        "kaynak_bbox": bbox,
        "kapsama_tam": covered,
        "deneme_sayisi": 0,
        "kayitli_item": source.get("kayitli_item") if source else None,
        "kayitli_rapor_tarihi": source.get("kayitli_rapor_tarihi") if source else None,
        "canli_item": source.get("canli_item") if source else None,
        "canli_datetime": source.get("canli_datetime") if source else None,
        "yeni_sahne": False,
    }

    if source is None or source.get("durum") != "ok":
        row["durum"] = "kaynak_hatasi"
        row["hata"] = (
            "Gülbahçe açık kapsama kontrolü için Uzunkuyu metadata yoklaması tamamlanamadı."
        )
        return row
    if not covered:
        row["durum"] = "kapsama_hatasi"
        row["hata"] = (
            "Uzunkuyu Sentinel kutusu Gülbahçe 2 km operasyon alanı + 150 m analiz "
            "bağlamını artık bütünüyle kapsamıyor."
        )
        return row

    row["durum"] = "ok"
    row["yeni_sahne"] = bool(source.get("yeni_sahne"))
    return row


def probe(connection, pair_provider=satellite.sentinel_pair):
    rows = []
    for region_key in STAC_REGIONS:
        stored_item, stored_date = _stored_latest_item(connection, region_key)
        attempts_used = 0
        try:
            (_older, latest), attempts_used = _probe_pair(pair_provider, region_key)
            live_item = latest.get("id")
            live_datetime = str(latest.get("properties", {}).get("datetime") or "")
            if not live_item:
                raise ValueError("Sentinel öğe kimliği boş")
            is_new = live_item != stored_item
            rows.append(
                {
                    "bolge": region_key,
                    "durum": "ok",
                    "metadata_sorgusu": True,
                    "deneme_sayisi": attempts_used,
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
                    "metadata_sorgusu": True,
                    "deneme_sayisi": attempts_used or 1,
                    "kayitli_item": stored_item,
                    "kayitli_rapor_tarihi": stored_date,
                    "canli_item": None,
                    "canli_datetime": None,
                    "yeni_sahne": False,
                    "hata": f"{type(exc).__name__}: {exc}",
                }
            )

    gulbahce_row = _gulbahce_coverage_row(rows)
    rows.append(gulbahce_row)
    successful_stac = [
        row
        for row in rows
        if row.get("metadata_sorgusu") is True and row.get("durum") == "ok"
    ]
    stac_complete = len(successful_stac) == len(STAC_REGIONS)
    probe_complete = stac_complete and gulbahce_row.get("durum") == "ok"
    return {
        "kontrol_zamani": datetime.now(ISTANBUL).isoformat(timespec="seconds"),
        "yeni_sahne": any(
            row.get("durum") == "ok" and row.get("yeni_sahne") for row in rows
        ),
        "probe_ok": bool(successful_stac),
        "probe_complete": probe_complete,
        "bolgeler": rows,
        "not": (
            "Bu yalnız metadata yoklamasıdır; alarm/görev üretmez. Yeni sahne saptanırsa "
            "mevcut tam tarama workflow'u çalıştırılmalıdır. Geçici STAC/ağ hataları "
            "bölge başına en fazla üç denemeyle sınırlandırılır. Gülbahçe, Uzunkuyu "
            "kutusunun 2 km operasyon alanı + 150 m analiz bağlamını kapsadığı ayrıca "
            "doğrulanan açık diagnostik kayıttır; ek STAC sorgusu yapılmaz."
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
        handle.write(f"probe_complete={'true' if payload['probe_complete'] else 'false'}\n")
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
        assert stable["probe_complete"] is True
        assert stable["yeni_sahne"] is False
        assert [row["bolge"] for row in stable["bolgeler"]] == [
            "cesme",
            "uzunkuyu",
            "gulbahce",
        ]
        gulbahce = stable["bolgeler"][-1]
        assert gulbahce["durum"] == "ok", gulbahce
        assert gulbahce["kapsama_tam"] is True, gulbahce
        assert gulbahce["metadata_sorgusu"] is False, gulbahce

        def one_changed(region_key):
            item_id = "CESME_NEW" if region_key == "cesme" else "UZUN_OLD"
            latest = {"id": item_id, "properties": {"datetime": "2026-09-05T09:00:00Z"}}
            return ({"id": "OLDER"}, latest)

        changed = probe(connection, one_changed)
        assert changed["yeni_sahne"] is True
        changed_rows = [row for row in changed["bolgeler"] if row["yeni_sahne"]]
        assert [row["bolge"] for row in changed_rows] == ["cesme"]

        # Geçici Timeout ilk denemede doğu bölgesini kör bırakmamalı; ikinci
        # denemede iyileşince aynı saat içinde yeni sahne görülebilmeli. Gülbahçe
        # aynı kaynak sorgudan açık kapsama kaydı olarak yeni sahneyi devralmalı.
        calls = {"cesme": 0, "uzunkuyu": 0}

        def transient_then_ok(region_key):
            calls[region_key] += 1
            if region_key == "uzunkuyu" and calls[region_key] == 1:
                raise requests.Timeout("gecici stac timeout")
            item_id = "CESME_OLD" if region_key == "cesme" else "UZUN_NEW"
            latest = {"id": item_id, "properties": {"datetime": "2026-09-05T09:00:00Z"}}
            return ({"id": "OLDER"}, latest)

        original_sleep = time.sleep
        try:
            time.sleep = lambda _seconds: None
            recovered = probe(connection, transient_then_ok)
        finally:
            time.sleep = original_sleep
        assert recovered["probe_complete"] is True
        assert recovered["yeni_sahne"] is True
        uzunkuyu = next(row for row in recovered["bolgeler"] if row["bolge"] == "uzunkuyu")
        gulbahce = next(row for row in recovered["bolgeler"] if row["bolge"] == "gulbahce")
        assert uzunkuyu["deneme_sayisi"] == 2, recovered
        assert gulbahce["yeni_sahne"] is True, recovered
        assert calls == {"cesme": 1, "uzunkuyu": 2}, calls

        def one_error(region_key):
            if region_key == "cesme":
                raise RuntimeError("kalici test hatasi")
            latest = {"id": "UZUN_OLD", "properties": {"datetime": "2026-09-03T09:00:00Z"}}
            return ({"id": "OLDER"}, latest)

        partial = probe(connection, one_error)
        assert partial["probe_ok"] is True
        assert partial["probe_complete"] is False
        assert partial["yeni_sahne"] is False
        assert partial["bolgeler"][0]["durum"] == "hata"
        assert partial["bolgeler"][0]["deneme_sayisi"] == 1

        # Gülbahçe doğu kenarı gelecekte daraltılırsa ana iki sorgu başarılı olsa
        # bile yoklama tamamlandı sayılmamalı; sessiz kapsama regresyonu görünür olmalı.
        original_bbox = list(satellite.REGIONS[GULBAHCE_SOURCE_REGION]["bbox"])
        try:
            satellite.REGIONS[GULBAHCE_SOURCE_REGION]["bbox"] = [26.45, 38.18, 26.65, 38.43]
            uncovered = probe(connection, unchanged)
        finally:
            satellite.REGIONS[GULBAHCE_SOURCE_REGION]["bbox"] = original_bbox
        gulbahce = next(row for row in uncovered["bolgeler"] if row["bolge"] == "gulbahce")
        assert gulbahce["durum"] == "kapsama_hatasi", uncovered
        assert gulbahce["kapsama_tam"] is False, uncovered
        assert uncovered["probe_complete"] is False, uncovered
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
    elif not payload["probe_complete"]:
        print("UYARI: Sentinel metadata yoklaması üretim bölgeleri veya Gülbahçe açık kapsama korumasında eksik tamamlandı.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())