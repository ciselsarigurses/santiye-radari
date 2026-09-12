"""Quality guard for the main Sentinel-1 RTC spatial-locality diagnostic.

This layer is diagnostic only. It verifies that strong RTC changes retain the
center-vs-surrounding locality evidence needed to distinguish compact local
intervention from broad surface/moisture/agricultural movement. It never
creates alarms or field tasks and does not alter the 250 m2 main threshold or
the 150-249 m2 MIKRO range.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import sentinel1_rtc_change_diagnostic as rtc

MIN_MAIN_M2 = 250
MICRO_RANGE_M2 = [150, 249]
EXPECTED_REGIONS = {"cesme", "uzunkuyu", "gulbahce"}
LOCAL = {"KOMPAKT_LOKAL_DESTEKLI", "LOKAL_AYRIM_DESTEKLI"}
BROAD = {
    "GENIS_CEVRE_HAREKETI_BASKIN",
    "GENIS_CEVRE_DEGISIMI_ESLIK_EDIYOR",
    "TEK_POL_CEVRE_DEGISIMI",
}


def _classify(target):
    metrics = target.get("polarizasyon_metrikleri") or {}
    locality = rtc._spatial_locality(metrics)
    status = locality.get("durum") or "VERI_YOK"
    try:
        score = float(target.get("sar_lokal_degisim_skor_db"))
    except (TypeError, ValueError):
        score = None

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
        "kaynak": target.get("kaynak"),
        "sar_lokal_degisim_skor_db": score,
        "sar_polarizasyon_uyumu": target.get("sar_polarizasyon_uyumu"),
        "sar_mekansal_ayrim": status,
        "yorum": meaning,
        "sar_cevre_degisim_konservatif_db": locality.get("cevre_konservatif_db"),
        "sar_cevre_degisim_tepe_db": locality.get("cevre_tepe_db"),
    }


def inspect_payload(payload):
    if payload.get("alarm") is not False or payload.get("saha_gorevi") is not False:
        raise ValueError("Main RTC diagnostic must stay alarm/task free")
    if int(payload.get("ana_sentinel_esigi_m2", MIN_MAIN_M2)) != MIN_MAIN_M2:
        raise ValueError("Main Sentinel threshold must remain 250 m2")
    if list(payload.get("mikro_aralik_m2", MICRO_RANGE_M2)) != MICRO_RANGE_M2:
        raise ValueError("MIKRO range must remain 150-249 m2")

    regions = payload.get("bolgeler") or []
    present = {str(r.get("bolge") or "").lower() for r in regions}
    missing = sorted(EXPECTED_REGIONS - present)
    if missing:
        raise ValueError(f"RTC locality diagnostic missing required regions: {missing}")

    rows = []
    for region in regions:
        region_name = str(region.get("bolge") or "").lower()
        for target in region.get("hedefler") or []:
            metrics = target.get("polarizasyon_metrikleri") or {}
            if not metrics:
                continue
            row = _classify(target)
            row["bolge"] = region_name
            score = row["sar_lokal_degisim_skor_db"]
            if score is not None and score >= 2.0 and row["sar_mekansal_ayrim"] == "VERI_YOK":
                raise ValueError("Strong main RTC diagnostic arrived without locality metrics")
            rows.append(row)

    strong_local = [
        row for row in rows
        if (row.get("sar_lokal_degisim_skor_db") or 0.0) >= 2.0 and row["yorum"] == "LOKAL_DESTEK"
    ]
    strong_broad = [
        row for row in rows
        if (row.get("sar_lokal_degisim_skor_db") or 0.0) >= 2.0 and row["yorum"] == "GENIS_YUZEY_RISKI"
    ]
    strong_local.sort(key=lambda r: r.get("sar_lokal_degisim_skor_db") or 0.0, reverse=True)
    strong_broad.sort(key=lambda r: r.get("sar_lokal_degisim_skor_db") or 0.0, reverse=True)

    return {
        "durum": "RTC_ANA_LOKALLIK_KAPISI_HAZIR",
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": MIN_MAIN_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "kapsanan_bolgeler": sorted(present & EXPECTED_REGIONS),
        "guclu_lokal_destekli_sayi": len(strong_local),
        "guclu_genis_yuzey_riski_sayi": len(strong_broad),
        "en_yuksek_guclu_lokal_destek": strong_local[:5],
        "en_yuksek_guclu_genis_yuzey_riski": strong_broad[:5],
        "not": "Bu kalite kapisi yalniz SAR lokallik kanitini denetler; tek basina insaat/kazi alarmi veya saha gorevi uretmez.",
    }


def self_check():
    compact = {
        "VV": {"mutlak_lokal_kontrast_degisim_db": 3.1, "mutlak_cevre_degisim_db": 0.3},
        "VH": {"mutlak_lokal_kontrast_degisim_db": 2.2, "mutlak_cevre_degisim_db": 0.7},
    }
    broad = {
        "VV": {"mutlak_lokal_kontrast_degisim_db": 3.0, "mutlak_cevre_degisim_db": 4.2},
        "VH": {"mutlak_lokal_kontrast_degisim_db": 2.4, "mutlak_cevre_degisim_db": 3.8},
    }
    payload = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "bolgeler": [
            {"bolge": "cesme", "hedefler": []},
            {"bolge": "uzunkuyu", "hedefler": []},
            {"bolge": "gulbahce", "hedefler": [
                {"enlem": 38.341406, "boylam": 26.643308, "alan_m2": 1200,
                 "sar_lokal_degisim_skor_db": 2.2, "sar_polarizasyon_uyumu": "CIFT_POL_GUCLU",
                 "polarizasyon_metrikleri": compact},
                {"enlem": 38.342310, "boylam": 26.642621, "alan_m2": 400,
                 "sar_lokal_degisim_skor_db": 2.4, "sar_polarizasyon_uyumu": "CIFT_POL_GUCLU",
                 "polarizasyon_metrikleri": broad},
            ]},
        ],
    }
    result = inspect_payload(payload)
    assert result["guclu_lokal_destekli_sayi"] == 1
    assert result["guclu_genis_yuzey_riski_sayi"] == 1
    assert result["alarm"] is False and result["saha_gorevi"] is False
    assert "gulbahce" in result["kapsanan_bolgeler"]
    print("Sentinel-1 RTC main locality guard self-check OK")


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
