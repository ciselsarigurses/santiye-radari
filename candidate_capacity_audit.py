"""Sentinel aday tavanının gerçek küçük/orta şantiye sinyallerini gizleyip gizlemediğini ölçer.

Bu denetim alarm üretmez, saha görevi açmaz ve uydu eşiklerini değiştirmez. Ana motorun
aynı 250 m² / 10 m / spektral kurallarıyla, yalnız aday sayısı tavanını geçici olarak
kaldırıp kaç uygun kümenin 24'lük çıktı listesinin dışında kaldığını sayar. Böylece
yoğun görüntülerde kota ayarı gerekiyorsa kanıta dayalı karar verilebilir.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np

import satellite
from daily_report import ISTANBUL, REPORT_REGIONS, ensure_daily_schema
from scanner import connect


AUDIT_FILE = Path(__file__).with_name("candidate_capacity_audit.json")
RAW_LIMIT = 1_000_000
CONSTRUCTION_SCALE_MIN_M2 = satellite.SMALL_HOTSPOT_MAX_M2
CONSTRUCTION_SCALE_MAX_M2 = 10_000

# analyze_sentinel_change çalışma anında satellite._hotspots adını çözer. Bu
# referansı saklayarak yalnız bu denetim sürecinde aday tavanını kaldırabiliyoruz;
# üretim algoritmasına veya DB'deki adaylara dokunulmuyor.
_ORIGINAL_HOTSPOTS = satellite._hotspots


def _uncapped_hotspots(
    change_mask,
    bbox,
    pixel_area_m2,
    small_site_mask=None,
    limit=satellite.HOTSPOT_LIMIT,
    small_quota=satellite.SMALL_HOTSPOT_QUOTA,
):
    del limit, small_quota
    return _ORIGINAL_HOTSPOTS(
        change_mask,
        bbox,
        pixel_area_m2,
        small_site_mask=small_site_mask,
        limit=RAW_LIMIT,
        small_quota=0,
    )


def _scale_bucket(item):
    try:
        area = float(item.get("alan_m2") or 0)
    except (TypeError, ValueError):
        area = 0.0
    if area < CONSTRUCTION_SCALE_MIN_M2:
        return "kucuk_250_800"
    if area <= CONSTRUCTION_SCALE_MAX_M2:
        return "santiye_olcegi_800_10000"
    return "genis_10000_ustu"


def _bucket_counts(items):
    counts = {
        "kucuk_250_800": 0,
        "santiye_olcegi_800_10000": 0,
        "genis_10000_ustu": 0,
    }
    for item in items:
        if isinstance(item, dict):
            counts[_scale_bucket(item)] += 1
    return counts


def _candidate_key(item):
    try:
        return (
            round(float(item.get("enlem")), 6),
            round(float(item.get("boylam")), 6),
            round(float(item.get("alan_m2") or 0)),
        )
    except (TypeError, ValueError, AttributeError):
        return None


def _self_check():
    # 30 ayrı güçlü ~300 m² küme üret. Üretim yolu 24'te kesilmeli; denetim yolu
    # aynı eşiklerle 30'un tamamını görmeli. Böylece denetimin yanlışlıkla yeni bir
    # algılama eşiği tanımlamadığı ve yalnız kapasite tavanını ölçtüğü doğrulanır.
    signal = np.zeros((61, 8), dtype=bool)
    for index in range(30):
        row = 1 + index * 2
        signal[row, 2:5] = True
    bbox = [26.30, 38.20, 26.31, 38.26]
    capped = _ORIGINAL_HOTSPOTS(
        signal,
        bbox,
        100.0,
        small_site_mask=signal,
    )
    uncapped = _uncapped_hotspots(
        signal,
        bbox,
        100.0,
        small_site_mask=signal,
    )
    assert len(capped) == satellite.HOTSPOT_LIMIT, (
        "Sentinel üretim aday tavanı beklenen 24'lük sınırda değil."
    )
    assert len(uncapped) == 30, (
        "Kapasite denetimi üretim eşiklerini değiştirmeden tavan dışı adayları göremiyor."
    )
    assert _scale_bucket({"alan_m2": 500}) == "kucuk_250_800"
    assert _scale_bucket({"alan_m2": 5000}) == "santiye_olcegi_800_10000"
    assert _scale_bucket({"alan_m2": 20000}) == "genis_10000_ustu"


def _stored_snapshot(report_date):
    snapshots = {}
    with connect() as connection:
        for region_key in REPORT_REGIONS:
            row = connection.execute(
                """SELECT son_item,hareket_json,hata FROM gunluk_uydu_raporlari
                WHERE rapor_tarihi=? AND bolge=? LIMIT 1""",
                (report_date, region_key),
            ).fetchone()
            if not row:
                snapshots[region_key] = {"son_item": None, "hareket": [], "hata": "rapor_yok"}
                continue
            try:
                movement = json.loads(row[1] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                movement = []
            if not isinstance(movement, list):
                movement = []
            snapshots[region_key] = {
                "son_item": row[0],
                "hareket": [item for item in movement if isinstance(item, dict)],
                "hata": row[2],
            }
    return snapshots


def audit_capacity():
    ensure_daily_schema()
    _self_check()
    now = datetime.now(ISTANBUL)
    report_date = now.strftime("%Y-%m-%d")
    stored = _stored_snapshot(report_date)
    regions = {}

    for region_key in REPORT_REGIONS:
        snapshot = stored.get(region_key, {})
        record = {
            "bolge": satellite.REGIONS[region_key]["label"],
            "son_item": snapshot.get("son_item"),
            "durum": "ok",
        }
        if snapshot.get("hata"):
            record["durum"] = "gunluk_uydu_hatasi"
            record["hata"] = str(snapshot.get("hata"))
            regions[region_key] = record
            continue
        try:
            older, latest = satellite.sentinel_pair(region_key)
            record["latest_item"] = latest.get("id")
            if snapshot.get("son_item") != latest.get("id"):
                record["durum"] = "gunluk_rapor_latest_ile_eslesmiyor"
                regions[region_key] = record
                continue

            original = satellite._hotspots
            satellite._hotspots = _uncapped_hotspots
            try:
                result = satellite.analyze_sentinel_change(
                    region_key,
                    pair=(older, latest),
                )
            finally:
                satellite._hotspots = original

            raw = [item for item in result.get("hotspots", []) if isinstance(item, dict)]
            kept = list(snapshot.get("hareket") or [])
            kept_keys = {key for key in map(_candidate_key, kept) if key is not None}
            dropped = [
                item for item in raw
                if (key := _candidate_key(item)) is not None and key not in kept_keys
            ]
            record.update(
                {
                    "aday_tavani": satellite.HOTSPOT_LIMIT,
                    "ham_uygun_aday": len(raw),
                    "uretim_listesindeki_aday": len(kept),
                    "tavan_disinda_kalan": len(dropped),
                    "tavana_ulasti": len(kept) >= satellite.HOTSPOT_LIMIT,
                    "ham_olcek_dagilimi": _bucket_counts(raw),
                    "uretim_olcek_dagilimi": _bucket_counts(kept),
                    "tavan_disinda_olcek_dagilimi": _bucket_counts(dropped),
                }
            )
        except Exception as exc:
            record["durum"] = "denetim_hatasi"
            record["hata"] = f"{type(exc).__name__}: {exc}"
        regions[region_key] = record

    payload = {
        "rapor_tarihi": report_date,
        "olusturma": now.strftime("%Y-%m-%d %H:%M %Z"),
        "amac": (
            "24 aday tavanının 250 m²+ geçerli Sentinel kümelerini gizleyip gizlemediğini "
            "ölçmek; bu dosya alarm veya saha görevi üretmez."
        ),
        "diagnostik_santiye_olcegi_m2": [
            CONSTRUCTION_SCALE_MIN_M2,
            CONSTRUCTION_SCALE_MAX_M2,
        ],
        "bolgeler": regions,
    }
    AUDIT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print(
            "Aday kapasite denetimi kalite kontrolü başarılı: üretim 24 tavanı korunuyor; "
            "denetim aynı 250 m²+ filtrelerle tavan dışı uygun kümeleri yalnız ölçüyor."
        )
        return

    payload = audit_capacity()
    summaries = []
    for key, item in payload["bolgeler"].items():
        if item.get("durum") != "ok":
            summaries.append(f"{key}: {item.get('durum')}")
            continue
        dropped_scale = item.get("tavan_disinda_olcek_dagilimi", {})
        summaries.append(
            f"{key}: {item.get('uretim_listesindeki_aday', 0)}/{item.get('ham_uygun_aday', 0)} "
            f"listede, tavan dışı {item.get('tavan_disinda_kalan', 0)} "
            f"(250-800={dropped_scale.get('kucuk_250_800', 0)}, "
            f"800-10000={dropped_scale.get('santiye_olcegi_800_10000', 0)}, "
            f">10000={dropped_scale.get('genis_10000_ustu', 0)})"
        )
    print("Aday kapasite denetimi: " + " | ".join(summaries))


if __name__ == "__main__":
    main()
