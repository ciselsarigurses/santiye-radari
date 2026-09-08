"""Global Sentinel bulut filtresine takılan daha yeni sahneleri saatlik metadata ile yakalar.

Üretim motoru güvenli ``eo:cloud_cover < 25`` eşiğini değiştirmez. Bu hafif katman
bant indirmez, alarm veya saha görevi üretmez. Earth Search metadata'sını daha geniş
bulut aralığında tarar; üretimde son işlenen sahneden daha yeni ve granül düzeyinde
%25+ bulutlu tam-kapsam bir sahne varsa mevcut ``cloud_threshold_audit`` workflow'unu
erken çalıştırmak için işaret üretir. Asıl yerel açıklık kararı orada SCL ile verilir.

Gülbahçe için üçüncü bir STAC sorgusu yapılmaz. Uzunkuyu kutusunda böyle bir sahne
bulunması, audit workflow'unda Gülbahçe 2 km operasyon penceresinin ayrıca SCL ile
ölçülmesini tetikler.
"""

from __future__ import annotations

import argparse
import json
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
PRODUCTION_MAX_CLOUD = 25.0
# satellite._search_items Earth Search'a ``eo:cloud_cover < max_cloud`` yollar.
# Sentinel metadata'sında geçerli üst değer %100 olabildiği için 100 kullanmak
# tam %100 kayıtları sessizce dışarıda bırakırdı. 100.01 yalnız bu strict-lt sınırını
# kapsar; üretim filtresini değiştirmez ve yerel SCL doğrulanmadan alarm üretmez.
BROAD_MAX_CLOUD = 100.01
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


def _item_time(item):
    return datetime.fromisoformat(
        str(item.get("properties", {}).get("datetime") or "").replace("Z", "+00:00")
    )


def _global_cloud(item):
    try:
        return float(item.get("properties", {}).get("eo:cloud_cover", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _retryable_error(exc):
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return status == 429 or (isinstance(status, int) and status >= 500)
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def _search_broad(
    search_provider,
    bbox,
    attempts=PROBE_ATTEMPTS,
    retry_seconds=PROBE_RETRY_SECONDS,
    sleep_fn=time.sleep,
):
    attempts = max(int(attempts), 1)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return search_provider(bbox, max_cloud=BROAD_MAX_CLOUD), attempt
        except Exception as exc:
            last_error = exc
            if attempt >= attempts or not _retryable_error(exc):
                raise
            delay = max(float(retry_seconds), 0.0) * attempt
            if delay:
                sleep_fn(delay)
    raise last_error


def _region_probe(connection, region_key, search_provider=satellite._search_items, sleep_fn=time.sleep):
    stored_item, stored_date = _stored_latest_item(connection, region_key)
    bbox = satellite.REGIONS[region_key]["bbox"]
    attempts_used = 0
    try:
        broad_items, attempts_used = _search_broad(
            search_provider,
            bbox,
            sleep_fn=sleep_fn,
        )
        _older, broad_latest = satellite._pick_pair(broad_items, bbox=bbox)
        broad_item = str(broad_latest.get("id") or "")
        if not broad_item:
            raise ValueError("Geniş bulut yoklamasında Sentinel öğe kimliği boş")

        stored_metadata = next(
            (item for item in broad_items if str(item.get("id") or "") == str(stored_item or "")),
            None,
        )
        if stored_item and stored_metadata is None:
            return {
                "bolge": region_key,
                "durum": "referans_yok",
                "deneme_sayisi": attempts_used,
                "kayitli_item": stored_item,
                "kayitli_rapor_tarihi": stored_date,
                "genis_item": broad_item,
                "genis_datetime": str(broad_latest.get("properties", {}).get("datetime") or ""),
                "genis_global_bulut": round(_global_cloud(broad_latest), 2),
                "cloud_audit_candidate": False,
                "hata": "Son işlenen Sentinel öğesi geniş metadata penceresinde bulunamadı; yanlış tetiklememek için aday üretilmedi.",
            }
        if not stored_item:
            return {
                "bolge": region_key,
                "durum": "referans_yok",
                "deneme_sayisi": attempts_used,
                "kayitli_item": None,
                "kayitli_rapor_tarihi": stored_date,
                "genis_item": broad_item,
                "genis_datetime": str(broad_latest.get("properties", {}).get("datetime") or ""),
                "genis_global_bulut": round(_global_cloud(broad_latest), 2),
                "cloud_audit_candidate": False,
                "hata": "Son işlenen Sentinel referansı yok; ilk üretim taraması tamamlanmadan bulut-körlük adayı üretilmedi.",
            }

        broad_time = _item_time(broad_latest)
        stored_time = _item_time(stored_metadata)
        broad_cloud = _global_cloud(broad_latest)
        candidate = broad_time > stored_time and broad_cloud >= PRODUCTION_MAX_CLOUD
        return {
            "bolge": region_key,
            "durum": "ok",
            "deneme_sayisi": attempts_used,
            "kayitli_item": stored_item,
            "kayitli_rapor_tarihi": stored_date,
            "kayitli_datetime": str(stored_metadata.get("properties", {}).get("datetime") or ""),
            "genis_item": broad_item,
            "genis_datetime": str(broad_latest.get("properties", {}).get("datetime") or ""),
            "genis_global_bulut": round(broad_cloud, 2),
            "cloud_audit_candidate": candidate,
        }
    except Exception as exc:
        return {
            "bolge": region_key,
            "durum": "hata",
            "deneme_sayisi": attempts_used or 1,
            "kayitli_item": stored_item,
            "kayitli_rapor_tarihi": stored_date,
            "genis_item": None,
            "genis_datetime": None,
            "genis_global_bulut": None,
            "cloud_audit_candidate": False,
            "hata": f"{type(exc).__name__}: {exc}",
        }


def probe(connection, search_provider=satellite._search_items, sleep_fn=time.sleep):
    rows = [
        _region_probe(connection, region_key, search_provider, sleep_fn)
        for region_key in STAC_REGIONS
    ]
    candidates = [
        row["bolge"]
        for row in rows
        if row.get("durum") == "ok" and row.get("cloud_audit_candidate")
    ]
    successful = [row for row in rows if row.get("durum") == "ok"]
    return {
        "kontrol_zamani": datetime.now(ISTANBUL).isoformat(timespec="seconds"),
        "cloud_audit_candidate": bool(candidates),
        "cloud_candidate_regions": candidates,
        "cloud_probe_ok": bool(successful),
        "cloud_probe_complete": len(successful) == len(STAC_REGIONS),
        "alarm": False,
        "saha_gorevi": False,
        "uretim_global_bulut_esigi_yuzde": PRODUCTION_MAX_CLOUD,
        "bolgeler": rows,
        "gulbahce_notu": "Uzunkuyu için global-bulut adayı çıkarsa mevcut audit Gülbahçe 2 km operasyon penceresini ayrıca SCL ile ölçer; bu yoklama üçüncü STAC sorgusu yapmaz.",
        "not": "Bu katman yalnız metadata erken uyarısıdır. 250 m² üretim eşiğini, 150-249 m² MİKRO politikasını veya saha görevlerini değiştirmez; yerel açıklık doğrulanmadan alarm üretmez.",
    }


def _write_github_output(path, payload):
    if not path:
        return
    target = Path(path)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(
            f"cloud_audit_candidate={'true' if payload['cloud_audit_candidate'] else 'false'}\n"
        )
        handle.write(
            f"cloud_probe_complete={'true' if payload['cloud_probe_complete'] else 'false'}\n"
        )
        handle.write(
            f"cloud_candidate_regions={','.join(payload['cloud_candidate_regions'])}\n"
        )


def _test_item(item_id, dt, cloud, bbox):
    return {
        "id": item_id,
        "bbox": list(bbox),
        "properties": {
            "datetime": dt,
            "eo:cloud_cover": cloud,
            "s2:mgrs_tile": "35SMC",
        },
    }


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
                ("2026-09-05", "cesme", "CESME_OLD"),
                ("2026-09-05", "uzunkuyu", "UZUN_OLD"),
            ],
        )

        cesme_bbox = satellite.REGIONS["cesme"]["bbox"]
        uzun_bbox = satellite.REGIONS["uzunkuyu"]["bbox"]

        def broad_with_cloudy_new(bbox, max_cloud=100):
            # Earth Search sorgusu strict ``lt`` kullandığı için üst sınırın %100
            # metadata değerini de içerecek kadar yüksek kaldığını doğrula.
            assert max_cloud == BROAD_MAX_CLOUD and max_cloud > 100
            if list(bbox) == list(cesme_bbox):
                return [
                    _test_item("CESME_OLD", "2026-09-05T09:00:00Z", 12, bbox),
                    _test_item("CESME_OLDER", "2026-09-03T09:00:00Z", 8, bbox),
                ]
            return [
                _test_item("UZUN_CLOUDY_NEW", "2026-09-07T09:00:00Z", 100, bbox),
                _test_item("UZUN_OLD", "2026-09-05T09:00:00Z", 10, bbox),
                _test_item("UZUN_OLDER", "2026-09-03T09:00:00Z", 9, bbox),
            ]

        result = probe(connection, broad_with_cloudy_new, sleep_fn=lambda _seconds: None)
        assert result["cloud_probe_complete"] is True, result
        assert result["cloud_audit_candidate"] is True, result
        assert result["cloud_candidate_regions"] == ["uzunkuyu"], result
        assert result["alarm"] is False and result["saha_gorevi"] is False

        # Daha yeni ama üretim eşiğinin altında kalan normal sahne bulut-audit adayı
        # değildir; standart Sentinel yoklaması zaten günlük taramayı tetikler.
        def broad_with_clear_new(bbox, max_cloud=100):
            stored = "CESME_OLD" if list(bbox) == list(cesme_bbox) else "UZUN_OLD"
            prefix = "CESME" if list(bbox) == list(cesme_bbox) else "UZUN"
            return [
                _test_item(f"{prefix}_CLEAR_NEW", "2026-09-07T09:00:00Z", 14, bbox),
                _test_item(stored, "2026-09-05T09:00:00Z", 10, bbox),
                _test_item(f"{prefix}_OLDER", "2026-09-03T09:00:00Z", 9, bbox),
            ]

        clear = probe(connection, broad_with_clear_new, sleep_fn=lambda _seconds: None)
        assert clear["cloud_probe_complete"] is True, clear
        assert clear["cloud_audit_candidate"] is False, clear

        calls = {"cesme": 0, "uzunkuyu": 0}

        def transient_then_ok(bbox, max_cloud=100):
            key = "cesme" if list(bbox) == list(cesme_bbox) else "uzunkuyu"
            calls[key] += 1
            if key == "uzunkuyu" and calls[key] == 1:
                raise requests.Timeout("gecici broad metadata timeout")
            item_id = "CESME_OLD" if key == "cesme" else "UZUN_OLD"
            return [
                _test_item(item_id, "2026-09-05T09:00:00Z", 10, bbox),
                _test_item(f"{key}_OLDER", "2026-09-03T09:00:00Z", 9, bbox),
            ]

        recovered = probe(connection, transient_then_ok, sleep_fn=lambda _seconds: None)
        assert recovered["cloud_probe_complete"] is True, recovered
        uzunkuyu = next(row for row in recovered["bolgeler"] if row["bolge"] == "uzunkuyu")
        assert uzunkuyu["deneme_sayisi"] == 2, recovered
        assert calls == {"cesme": 1, "uzunkuyu": 2}, calls
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-output", default="")
    parser.add_argument("--self-check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if args.self_check_only:
        print("Global bulut metadata körlüğü yoklama öz testi başarılı.")
        return 0

    with sqlite3.connect(DB_PATH) as connection:
        payload = probe(connection)
    _write_github_output(args.github_output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["cloud_probe_complete"]:
        print("UYARI: Global bulut metadata körlüğü yoklaması tüm üretim bölgelerinde tamamlanamadı; standart Sentinel yoklaması bundan bağımsızdır.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())