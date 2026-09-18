"""Sentinel-1'de yuksek sinyalin kompakt-lokal hale gelmesini ayiran diagnostik katman.

Bu kontrol yeni alarm veya saha gorevi uretmez. Amaci, onceki RTC araliginda da
yuksek sinyal varken temporal siniflandirici tarafindan karisik/dusuk kanit olarak
kalan; buna karsin en guncel aralikta cift-polarizasyon guclu ve kompakt-lokal
hale gelen hedefleri kaybetmemektir. Bu desen erken hafriyat icin ek inceleme
kaniti olabilir, fakat tek basina insaat/temel kazisi kaniti degildir.

250 m2 ana esigi korunur. 150-249 m2 MIKRO hedefler ancak zaten
mikro_guclu_diagnostik olarak isaretliyse bu diagnostige girebilir.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

STRONG_LOCAL_DB = 2.0
MIN_MAIN_M2 = 250
MICRO_MIN_M2 = 150
MICRO_MAX_M2 = 249
LOCAL_CLASSES = {"KOMPAKT_LOKAL_DESTEKLI", "LOKAL_AYRIM_DESTEKLI"}
STRONG_POL_CLASSES = {"CIFT_POL_GUCLU"}
MIXED_TEMPORAL_CLASS = "TEMPORAL_KARISIK_DUSUK_KANIT"
OUTPUT_CLASS = "LOKALLIK_KESKINLESME_ADAYI"


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def classify_feature(feature):
    props = feature.get("properties") or {}
    area = _float(props.get("alan_m2"))
    current = _float(props.get("sar_lokal_degisim_skor_db"))
    previous = _float(props.get("onceki_sar_lokal_degisim_skor_db"))
    locality = str(props.get("sar_mekansal_ayrim") or "")
    pol = str(props.get("sar_polarizasyon_uyumu") or "")
    temporal = str(props.get("temporal_durum") or "")

    if area is None or current is None or previous is None:
        return None
    if current < STRONG_LOCAL_DB or previous < STRONG_LOCAL_DB:
        return None
    if locality not in LOCAL_CLASSES or pol not in STRONG_POL_CLASSES:
        return None
    if temporal != MIXED_TEMPORAL_CLASS:
        return None

    if area >= MIN_MAIN_M2:
        scale = "ANA_250_PLUS"
    elif MICRO_MIN_M2 <= area <= MICRO_MAX_M2 and bool(props.get("mikro_guclu_diagnostik")):
        scale = "MIKRO_DIAGNOSTIK"
    else:
        return None

    region = str(props.get("bolge") or "").lower()
    return {
        "id": feature.get("id"),
        "enlem": (feature.get("geometry") or {}).get("coordinates", [None, None])[1],
        "boylam": (feature.get("geometry") or {}).get("coordinates", [None, None])[0],
        "alan_m2": area,
        "olcek": scale,
        "bolge": region,
        "guncel_skor_db": current,
        "onceki_skor_db": previous,
        "guncel_mekansal_ayrim": locality,
        "guncel_polarizasyon_uyumu": pol,
        "onceki_temporal_sinif": temporal,
        "diagnostik_sinif": OUTPUT_CLASS,
        "operasyonel_oncelik": region != "gulbahce",
        "alarm": False,
        "saha_gorevi": False,
    }


def inspect_payload(payload):
    rows = []
    for feature in payload.get("features") or []:
        row = classify_feature(feature)
        if row:
            rows.append(row)
    return {
        "politika": (
            "DIAGNOSTIK_ONLY: yuksek onceki SAR sinyalinin guncel aralikta kompakt-lokal "
            "hale gelmesini ayirir; tek basina kazı/insaat kaniti, alarm veya saha gorevi degildir."
        ),
        "ana_sentinel_esigi_m2": MIN_MAIN_M2,
        "mikro_aralik_m2": [MICRO_MIN_M2, MICRO_MAX_M2],
        "alarm": False,
        "saha_gorevi": False,
        "lokallik_keskinlesme_adayi": len(rows),
        "operasyonel_oncelikli_aday": sum(1 for row in rows if row["operasyonel_oncelik"]),
        "adaylar": rows,
    }


def _synthetic_feature(*, area=900, current=3.0, previous=4.0, locality="KOMPAKT_LOKAL_DESTEKLI", pol="CIFT_POL_GUCLU", temporal=MIXED_TEMPORAL_CLASS, micro=False, region="cesme"):
    return {
        "id": "test",
        "geometry": {"type": "Point", "coordinates": [26.3, 38.3]},
        "properties": {
            "alan_m2": area,
            "sar_lokal_degisim_skor_db": current,
            "onceki_sar_lokal_degisim_skor_db": previous,
            "sar_mekansal_ayrim": locality,
            "sar_polarizasyon_uyumu": pol,
            "temporal_durum": temporal,
            "mikro_guclu_diagnostik": micro,
            "bolge": region,
        },
    }


def self_test():
    assert classify_feature(_synthetic_feature()) is not None
    assert classify_feature(_synthetic_feature(previous=0.8)) is None
    assert classify_feature(_synthetic_feature(locality="KARISIK_DUSUK_LOKALLIK")) is None
    assert classify_feature(_synthetic_feature(pol="CIFT_POL_ORTA")) is None
    assert classify_feature(_synthetic_feature(temporal="ARDISIK_GUCLU_LOKAL_HAREKET")) is None
    assert classify_feature(_synthetic_feature(area=200, micro=False)) is None
    micro = classify_feature(_synthetic_feature(area=200, micro=True))
    assert micro is not None and micro["olcek"] == "MIKRO_DIAGNOSTIK"
    gulbahce = classify_feature(_synthetic_feature(region="gulbahce"))
    assert gulbahce is not None and gulbahce["operasyonel_oncelik"] is False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="sentinel1_rtc_map.geojson")
    parser.add_argument("--output")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        print("SELF_TEST_OK")
        return

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    result = inspect_payload(payload)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
