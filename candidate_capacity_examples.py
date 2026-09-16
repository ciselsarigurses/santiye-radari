"""24 aday tavanı / rapor seçimi dışında kalan Sentinel adaylarını görünür kılar.

Bu dosya yalnız diagnostiktir: alarm üretmez, saha görevi açmaz, 250 m² ana eşiği
ve 150–249 m² MİKRO politikasını değiştirmez. ``candidate_capacity_audit`` zaten
kaç aday kaybedildiğini sayıyor; burada aynı ölçümden küçük ve kararlı bir örnek
listesi üretilir ki tavan dışında kalan gerçek parsel-ölçeği adaylar koordinatsız
bir sayaç olarak kaybolmasın.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import candidate_capacity_audit as capacity
import satellite
from daily_report import ISTANBUL, REPORT_REGIONS, ensure_daily_schema


OUTPUT_FILE = Path(__file__).with_name("candidate_capacity_examples.json")
EXAMPLE_LIMIT = 8
SAFE_KEYS = (
    "enlem",
    "boylam",
    "alan_m2",
    "boyut_sinifi",
    "sinyal",
    "tarim_riski",
    "tarim_baglam_alani_m2",
    "tarim_baglam_orani",
)


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _example(item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in SAFE_KEYS:
        value = item.get(key)
        if value is None:
            continue
        result[key] = value

    latitude = _number(item.get("enlem"))
    longitude = _number(item.get("boylam"))
    area = _number(item.get("alan_m2"))
    if latitude is not None:
        result["enlem"] = round(latitude, 6)
    if longitude is not None:
        result["boylam"] = round(longitude, 6)
    if area is not None:
        result["alan_m2"] = round(area)
    if latitude is not None and longitude is not None:
        result["harita"] = (
            "https://www.google.com/maps/search/?api=1&query="
            f"{latitude:.6f},{longitude:.6f}"
        )
    return result


def _examples(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_example(item) for item in items[:EXAMPLE_LIMIT] if isinstance(item, dict)]


def build_examples() -> dict[str, Any]:
    ensure_daily_schema()
    report_date = ISTANBUL.localize if False else None  # timezone nesnesini yanlışlıkla dönüştürme koruması
    del report_date
    # candidate_capacity_audit ile aynı yerel günü kullan; çıktı sahne değişmedikçe
    # timestamp yüzünden her çalışmada değişmesin.
    from datetime import datetime

    report_date = datetime.now(ISTANBUL).strftime("%Y-%m-%d")
    stored = capacity._stored_snapshot(report_date)
    regions: dict[str, Any] = {}

    for region_key in REPORT_REGIONS:
        snapshot = stored.get(region_key, {})
        record: dict[str, Any] = {
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
            pair = satellite.sentinel_pair(region_key)
            _older, latest = pair
            latest_id = latest.get("id")
            record["latest_item"] = latest_id
            if snapshot.get("son_item") != latest_id:
                record["durum"] = "gunluk_rapor_latest_ile_eslesmiyor"
                regions[region_key] = record
                continue

            capped_result = capacity._analyze_with_hotspot_function(
                region_key, pair, capacity._ORIGINAL_HOTSPOTS
            )
            raw_result = capacity._analyze_with_hotspot_function(
                region_key, pair, capacity._uncapped_hotspots
            )
            capped = [
                item
                for item in (capped_result.get("hotspots") or [])
                if isinstance(item, dict)
            ]
            raw = [
                item
                for item in (raw_result.get("hotspots") or [])
                if isinstance(item, dict)
            ]
            report_kept = [
                item
                for item in (snapshot.get("hareket") or [])
                if isinstance(item, dict)
            ]
            dropped_by_cap = capacity._difference(raw, capped)
            dropped_by_report_selection = capacity._difference(capped, report_kept)

            record.update(
                {
                    "ana_sentinel_esigi_m2": satellite.MIN_HOTSPOT_AREA_M2,
                    "aday_tavani": satellite.HOTSPOT_LIMIT,
                    "ham_uygun_aday": len(raw),
                    "tavan_sonrasi_aday": len(capped),
                    "raporda_kalan_aday": len(report_kept),
                    "tavan_disinda_kalan": len(dropped_by_cap),
                    "tavan_disinda_ornekler": _examples(dropped_by_cap),
                    "rapor_secimi_disinda_kalan": len(dropped_by_report_selection),
                    "rapor_secimi_disinda_ornekler": _examples(
                        dropped_by_report_selection
                    ),
                }
            )
        except Exception as exc:
            record["durum"] = "denetim_hatasi"
            record["hata"] = f"{type(exc).__name__}: {exc}"
        regions[region_key] = record

    return {
        "rapor_tarihi": report_date,
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": satellite.MIN_HOTSPOT_AREA_M2,
        "mikro_aralik_m2": [150, 249],
        "ornek_limit": EXAMPLE_LIMIT,
        "amac": (
            "Kapasite tavanı veya rapor seçimi dışında kalan 250 m²+ Sentinel "
            "adaylarının koordinatlarını diagnostik olarak görünür tutmak; alarm/görev "
            "ve üretim eşiği değiştirilmez."
        ),
        "bolgeler": regions,
    }


def _self_check() -> None:
    sample = {
        "enlem": "38.12345649",
        "boylam": 26.65432149,
        "alan_m2": 1499.6,
        "boyut_sinifi": "STANDART",
        "sinyal": "test",
        "tarim_riski": False,
        "bilinmeyen": "çıktıya taşınmamalı",
    }
    row = _example(sample)
    assert row["enlem"] == 38.123456
    assert row["boylam"] == 26.654321
    assert row["alan_m2"] == 1500
    assert row["boyut_sinifi"] == "STANDART"
    assert "bilinmeyen" not in row
    assert row["harita"].endswith("38.123456,26.654321")
    assert _examples([sample] * 20).__len__() == EXAMPLE_LIMIT


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print(
            "Kapasite örnek diagnostik öz testi başarılı; eşik/alarm/görev değişmiyor."
        )
        return

    payload = build_examples()
    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "Kapasite örnek diagnostik üretildi: "
        + " | ".join(
            f"{key}: tavan dışı={row.get('tavan_disinda_kalan', 0)}, "
            f"rapor dışı={row.get('rapor_secimi_disinda_kalan', 0)}"
            for key, row in payload["bolgeler"].items()
        )
    )


if __name__ == "__main__":
    main()
