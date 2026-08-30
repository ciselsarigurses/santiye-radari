"""Sentinel-2 reflektans ölçek/offset metadata varsayımını denetler.

Üretim uydu motoru red/nir DN değerlerini şu anda 0.0001 ölçek ve -0.1 offset
varsayımıyla reflektansa çeviriyor. Earth Search STAC varlıkları ise ölçek/offset
bilgisini varlık bazında ``raster:bands`` metadata'sında taşıyabilir. Additif offset
NDVI hesabında sadeleşmediği için yanlış varsayım, özellikle küçük hafriyat yolunda
vegetation-loss ve latest-NDVI eşiklerini kaydırabilir.

Bu dosya üretim alarmını, eşiğini veya saha görevini DEĞİŞTİRMEZ. Seçili güncel
Sentinel çiftlerinin red/nir metadata'sını ölçer ve hard-coded varsayımla uyuşup
uyuşmadığını görünür kılar. Kesin bir MISMATCH varsa yanlış NDVI ile yeni rapor
üretilmemesi için taramayı güvenli biçimde başarısız sonlandırır. Metadata eksikse
UNKNOWN olarak uyarır fakat veri kaynağını gereksiz yere kör bırakmaz.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import satellite


REPORT_FILE = Path(__file__).with_name("reflectance_metadata_audit.json")
EXPECTED_SCALE = 0.0001
EXPECTED_OFFSET = -0.1
BANDS = ("red", "nir")
REGION_KEYS = ("cesme", "uzunkuyu")
TOLERANCE = 1e-12


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scale_offset_from_asset(asset):
    if not isinstance(asset, dict):
        return None, None, "asset_missing"
    bands = asset.get("raster:bands")
    if not isinstance(bands, list) or not bands or not isinstance(bands[0], dict):
        return None, None, "raster_bands_missing"
    band = bands[0]
    scale = _number(band.get("scale"))
    offset = _number(band.get("offset"))
    if scale is None or offset is None:
        return scale, offset, "scale_or_offset_missing"
    return scale, offset, "ok"


def _matches_expected(scale, offset):
    if scale is None or offset is None:
        return None
    return (
        abs(scale - EXPECTED_SCALE) <= TOLERANCE
        and abs(offset - EXPECTED_OFFSET) <= TOLERANCE
    )


def _inspect_item(item):
    assets = item.get("assets", {}) if isinstance(item, dict) else {}
    bands = {}
    statuses = []
    for band_name in BANDS:
        scale, offset, metadata_status = _scale_offset_from_asset(assets.get(band_name))
        matches = _matches_expected(scale, offset)
        bands[band_name] = {
            "scale": scale,
            "offset": offset,
            "metadata_status": metadata_status,
            "hardcoded_varsayimla_uyumlu": matches,
        }
        statuses.append(matches)

    if any(status is False for status in statuses):
        item_status = "MISMATCH"
    elif all(status is True for status in statuses):
        item_status = "MATCH"
    else:
        item_status = "UNKNOWN"

    properties = item.get("properties", {}) if isinstance(item, dict) else {}
    return {
        "item": str(item.get("id") or "-"),
        "tarih": str(properties.get("datetime") or "-"),
        "mgrs": str(properties.get("s2:mgrs_tile") or "-"),
        "relative_orbit": satellite._relative_orbit(item),
        "durum": item_status,
        "bantlar": bands,
    }


def _dedupe_items(region_pairs):
    unique = {}
    for pair in region_pairs.values():
        for item in pair:
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            unique[item_id] = item
    return unique


def audit():
    warnings = []
    region_pairs = {}
    regions = {}

    for region_key in REGION_KEYS:
        try:
            older, latest = satellite.sentinel_pair(region_key)
        except Exception as exc:
            warnings.append(f"{region_key}: {type(exc).__name__}: {exc}")
            regions[region_key] = {"durum": "ERROR", "hata": str(exc)}
            continue
        region_pairs[region_key] = (older, latest)
        regions[region_key] = {
            "durum": "ok",
            "onceki_item": str(older.get("id") or "-"),
            "son_item": str(latest.get("id") or "-"),
        }

    items = {
        item_id: _inspect_item(item)
        for item_id, item in sorted(_dedupe_items(region_pairs).items())
    }
    item_statuses = [row["durum"] for row in items.values()]
    if any(status == "MISMATCH" for status in item_statuses):
        overall = "MISMATCH"
    elif item_statuses and all(status == "MATCH" for status in item_statuses):
        overall = "MATCH"
    elif warnings and not items:
        overall = "ERROR"
    else:
        overall = "UNKNOWN"

    return {
        "amac": (
            "Seçili Sentinel-2 red/nir raster:bands scale-offset metadata'sını üretim "
            "reflektans varsayımıyla karşılaştırmak; alarm veya görev üretmez."
        ),
        "uretim_varsayimi": {
            "scale": EXPECTED_SCALE,
            "offset": EXPECTED_OFFSET,
            "formul": "reflectance = DN * scale + offset",
        },
        "genel_durum": overall,
        "bolgeler": regions,
        "itemlar": items,
        "uyarilar": warnings,
    }


def _should_block(payload):
    """Yalnız kesin metadata uyumsuzluğunda yanlış NDVI üretimini durdur."""
    return isinstance(payload, dict) and payload.get("genel_durum") == "MISMATCH"


def _self_check():
    exact = {"raster:bands": [{"scale": 0.0001, "offset": -0.1}]}
    scale, offset, status = _scale_offset_from_asset(exact)
    assert status == "ok"
    assert _matches_expected(scale, offset) is True

    zero_offset = {"raster:bands": [{"scale": 0.0001, "offset": 0.0}]}
    scale, offset, status = _scale_offset_from_asset(zero_offset)
    assert status == "ok"
    assert _matches_expected(scale, offset) is False

    missing = {"raster:bands": [{}]}
    scale, offset, status = _scale_offset_from_asset(missing)
    assert status == "scale_or_offset_missing"
    assert _matches_expected(scale, offset) is None

    assert _scale_offset_from_asset({})[2] == "raster_bands_missing"
    assert _should_block({"genel_durum": "MISMATCH"}) is True
    assert _should_block({"genel_durum": "MATCH"}) is False
    assert _should_block({"genel_durum": "UNKNOWN"}) is False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if args.check_only:
        print(
            "Reflektans metadata denetimi öz testi başarılı: scale/offset eşleşme, "
            "uyuşmazlık ve eksik metadata yolları ayrıştırılıyor; kesin uyumsuzluk "
            "yanlış NDVI ile taramaya devam etmiyor."
        )
        return

    payload = audit()
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    current = REPORT_FILE.read_text(encoding="utf-8") if REPORT_FILE.exists() else None
    if current != text:
        REPORT_FILE.write_text(text, encoding="utf-8")

    match_count = sum(row["durum"] == "MATCH" for row in payload["itemlar"].values())
    mismatch_count = sum(row["durum"] == "MISMATCH" for row in payload["itemlar"].values())
    unknown_count = sum(row["durum"] == "UNKNOWN" for row in payload["itemlar"].values())
    print(
        "Sentinel reflektans metadata denetimi: "
        f"genel={payload['genel_durum']}; item MATCH={match_count}, "
        f"MISMATCH={mismatch_count}, UNKNOWN={unknown_count}."
    )
    if mismatch_count:
        print(
            "DİKKAT: Seçili Sentinel varlığında raster:bands scale/offset, üretimdeki "
            "hard-coded 0.0001/-0.1 varsayımından farklı. Yanlış NDVI ile yeni saha "
            "adayı üretilmemesi için bu tarama güvenli biçimde durdurulacak."
        )
    if unknown_count:
        print(
            "DİKKAT: Bazı seçili Sentinel varlıklarında scale/offset metadata'sı eksik; "
            "üretim dönüşümü değiştirilmeden önce veri semantiği ayrıca doğrulanmalı."
        )
    if payload["uyarilar"]:
        print("Metadata denetim uyarıları: " + " | ".join(payload["uyarilar"]))

    if _should_block(payload):
        raise SystemExit(
            "Sentinel reflektans scale/offset metadata uyumsuzluğu: yanlış NDVI ile "
            "radar raporu üretilmedi."
        )


if __name__ == "__main__":
    main()
