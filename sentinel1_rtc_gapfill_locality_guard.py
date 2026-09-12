"""Quality guard for Sentinel-1 RTC gapfill locality diagnostics.

Diagnostic only: never creates an alarm or field task. It separates compact
local change from broad surrounding movement using metrics already produced by
the gapfill layer. Main 250 m2 and MIKRO 150-249 m2 policies stay unchanged.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import sentinel1_rtc_change_diagnostic as rtc

MIN_MAIN_M2 = 250
MICRO_RANGE_M2 = [150, 249]
LOCAL = {"KOMPAKT_LOKAL_DESTEKLI", "LOKAL_AYRIM_DESTEKLI"}
BROAD = {"GENIS_CEVRE_HAREKETI_BASKIN", "GENIS_CEVRE_DEGISIMI_ESLIK_EDIYOR", "TEK_POL_CEVRE_DEGISIMI"}


def classify_target(target):
    locality = rtc._spatial_locality(target.get("polarizasyon_metrikleri") or {})
    status = locality.get("durum") or "VERI_YOK"
    if status in LOCAL:
        meaning = "LOKAL_DESTEK"
    elif status in BROAD:
        meaning = "GENIS_YUZEY_RISKI"
    elif status == "VERI_YOK":
        meaning = "LOKALLIK_VERISI_YOK"
    else:
        meaning = "KARISIK_DUSUK_LOKALLIK"
    return {
        "enlem": target.get("enlem"),
        "boylam": target.get("boylam"),
        "alan_m2": target.get("alan_m2"),
        "rtc_tazelik": target.get("rtc_tazelik"),
        "sar_lokal_degisim_skor_db": target.get("sar_lokal_degisim_skor_db"),
        "sar_mekansal_ayrim": status,
        "yorum": meaning,
        "sar_cevre_degisim_konservatif_db": locality.get("cevre_konservatif_db"),
        "sar_cevre_degisim_tepe_db": locality.get("cevre_tepe_db"),
    }


def inspect_payload(payload):
    if payload.get("alarm") is not False or payload.get("saha_gorevi") is not False:
        raise ValueError("Gapfill must stay diagnostic only")
    if int(payload.get("ana_sentinel_esigi_m2", MIN_MAIN_M2)) != MIN_MAIN_M2:
        raise ValueError("Main Sentinel threshold must remain 250 m2")
    if list(payload.get("mikro_aralik_m2", MICRO_RANGE_M2)) != MICRO_RANGE_M2:
        raise ValueError("MIKRO range must remain 150-249 m2")

    rows = []
    for region in payload.get("bolgeler") or []:
        for target in region.get("hedefler") or []:
            metrics = target.get("polarizasyon_metrikleri") or {}
            if not metrics:
                continue
            row = classify_target(target)
            score = row.get("sar_lokal_degisim_skor_db")
            try:
                score = float(score) if score is not None else None
            except (TypeError, ValueError):
                score = None
            row["sar_lokal_degisim_skor_db"] = score
            if score is not None and score >= 2.0 and row["sar_mekansal_ayrim"] == "VERI_YOK":
                raise ValueError("Strong gapfill diagnostic arrived without locality metrics")
            rows.append(row)

    fresh_local = [r for r in rows if r.get("rtc_tazelik") == "RTC_GUNCEL" and r["yorum"] == "LOKAL_DESTEK"]
    fresh_broad = [r for r in rows if r.get("rtc_tazelik") == "RTC_GUNCEL" and r["yorum"] == "GENIS_YUZEY_RISKI"]
    fresh_local.sort(key=lambda r: r.get("sar_lokal_degisim_skor_db") or 0.0, reverse=True)
    fresh_broad.sort(key=lambda r: r.get("sar_lokal_degisim_skor_db") or 0.0, reverse=True)
    return {
        "durum": "GAPFILL_LOKALLIK_KAPISI_HAZIR",
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": MIN_MAIN_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "taze_lokal_destekli_sayi": len(fresh_local),
        "taze_genis_yuzey_riski_sayi": len(fresh_broad),
        "en_yuksek_taze_lokal_destek": fresh_local[:5],
        "en_yuksek_taze_genis_yuzey_riski": fresh_broad[:5],
    }


def self_check():
    compact = {
        "VV": {"mutlak_lokal_kontrast_degisim_db": 2.8, "mutlak_cevre_degisim_db": 0.4},
        "VH": {"mutlak_lokal_kontrast_degisim_db": 2.2, "mutlak_cevre_degisim_db": 0.6},
    }
    broad = {
        "VV": {"mutlak_lokal_kontrast_degisim_db": 2.6, "mutlak_cevre_degisim_db": 4.1},
        "VH": {"mutlak_lokal_kontrast_degisim_db": 2.1, "mutlak_cevre_degisim_db": 3.7},
    }
    payload = {
        "alarm": False, "saha_gorevi": False,
        "ana_sentinel_esigi_m2": 250, "mikro_aralik_m2": [150, 249],
        "bolgeler": [{"hedefler": [
            {"enlem": 38.341406, "boylam": 26.643308, "alan_m2": 1200, "rtc_tazelik": "RTC_GUNCEL", "sar_lokal_degisim_skor_db": 2.2, "polarizasyon_metrikleri": compact},
            {"enlem": 38.342310, "boylam": 26.642621, "alan_m2": 400, "rtc_tazelik": "RTC_GUNCEL", "sar_lokal_degisim_skor_db": 2.1, "polarizasyon_metrikleri": broad},
        ]}],
    }
    result = inspect_payload(payload)
    assert result["taze_lokal_destekli_sayi"] == 1
    assert result["taze_genis_yuzey_riski_sayi"] == 1
    assert result["alarm"] is False and result["saha_gorevi"] is False
    print("Sentinel-1 RTC gapfill locality guard self-check OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check-only", action="store_true")
    parser.add_argument("--input")
    args = parser.parse_args()
    if args.self_check_only:
        self_check()
        return
    if not args.input:
        parser.error("--input is required")
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    print(json.dumps(inspect_payload(payload), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
