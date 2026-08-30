"""Sentinel aday geometrisinde geniş/lineer yanlış-pozitif riskini ölçer.

Bu denetim alarm üretmez, saha görevi açmaz ve üretim eşiklerini değiştirmez.
Ana Sentinel motorunun oluşturduğu aynı değişim maskesindeki geçerli 250 m²+ kümelerin
şekil özelliklerini ölçer. Amaç özellikle yol, altyapı veya uzun şerit biçimli yüzey
değişimlerinin >10.000 m² geniş aday havuzunu baskılayıp baskılamadığını sayısal olarak
görmektir. "Uzun-ince" veya "düşük kompaktlık" etiketi tek başına yol/yanlış pozitif
kanıtı değildir; yalnız saha geri bildirimiyle ileride güvenli bir sıralama kuralı
kalibre edilebilmesi için diagnostik sinyaldir.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

import satellite
from daily_report import ISTANBUL, REPORT_REGIONS, ensure_daily_schema
from scanner import connect


AUDIT_FILE = Path(__file__).with_name("shape_false_positive_audit.json")
LONG_THIN_ASPECT_MIN = 5.0
LOW_COMPACTNESS_MAX = 0.15
EXAMPLE_LIMIT = 6
_ORIGINAL_HOTSPOTS = satellite._hotspots


def _area_bucket(area_m2):
    if area_m2 < satellite.SMALL_HOTSPOT_MAX_M2:
        return "kucuk_250_800"
    if area_m2 <= 10_000:
        return "santiye_olcegi_800_10000"
    return "genis_10000_ustu"


def _eligible(component, pixel_area_m2, small_site_mask=None):
    area_m2 = len(component) * pixel_area_m2
    if area_m2 < satellite.MIN_HOTSPOT_AREA_M2:
        return False
    if area_m2 >= satellite.SMALL_HOTSPOT_MAX_M2:
        return True
    if small_site_mask is None:
        return False
    pixels = np.asarray(component, dtype="int32")
    strong_fraction = float(
        np.mean(small_site_mask[pixels[:, 0], pixels[:, 1]])
    )
    return strong_fraction >= 0.50


def _component_location(component, bbox, shape):
    pixels = np.asarray(component, dtype="int32")
    centroid = pixels.mean(axis=0)
    distance = np.sum((pixels - centroid) ** 2, axis=1)
    representative = pixels[int(np.argmin(distance))]
    row, column = int(representative[0]), int(representative[1])
    height, width = shape
    west, south, east, north = bbox
    latitude = north - (row + 0.5) / height * (north - south)
    longitude = west + (column + 0.5) / width * (east - west)
    return round(latitude, 6), round(longitude, 6)


def _perimeter_edges(component):
    pixels = set(component)
    exposed = 0
    for row, column in component:
        exposed += (row - 1, column) not in pixels
        exposed += (row + 1, column) not in pixels
        exposed += (row, column - 1) not in pixels
        exposed += (row, column + 1) not in pixels
    return max(int(exposed), 1)


def _shape_record(component, bbox, shape, pixel_area_m2):
    pixels = np.asarray(component, dtype="int32")
    rows = pixels[:, 0]
    columns = pixels[:, 1]
    row_span = int(rows.max() - rows.min() + 1)
    col_span = int(columns.max() - columns.min() + 1)
    short_span = max(min(row_span, col_span), 1)
    long_span = max(row_span, col_span)
    aspect_ratio = float(long_span / short_span)
    fill_ratio = float(len(component) / max(row_span * col_span, 1))
    perimeter = _perimeter_edges(component)
    compactness = float(4 * math.pi * len(component) / (perimeter * perimeter))
    latitude, longitude = _component_location(component, bbox, shape)
    area_m2 = round(len(component) * pixel_area_m2)
    return {
        "enlem": latitude,
        "boylam": longitude,
        "alan_m2": area_m2,
        "olcek": _area_bucket(area_m2),
        "piksel": len(component),
        "satir_span": row_span,
        "sutun_span": col_span,
        "uzun_kisa_orani": round(aspect_ratio, 2),
        "kutu_doluluk_orani": round(fill_ratio, 3),
        "kompaktlik": round(compactness, 3),
        "uzun_ince": aspect_ratio >= LONG_THIN_ASPECT_MIN,
        "dusuk_kompaktlik": compactness <= LOW_COMPACTNESS_MAX,
    }


def _candidate_key(item):
    try:
        return (
            round(float(item.get("enlem")), 6),
            round(float(item.get("boylam")), 6),
            round(float(item.get("alan_m2") or 0)),
        )
    except (TypeError, ValueError, AttributeError):
        return None


def _summarize(records):
    buckets = {
        "kucuk_250_800": {"toplam": 0, "uzun_ince": 0, "dusuk_kompaktlik": 0},
        "santiye_olcegi_800_10000": {"toplam": 0, "uzun_ince": 0, "dusuk_kompaktlik": 0},
        "genis_10000_ustu": {"toplam": 0, "uzun_ince": 0, "dusuk_kompaktlik": 0},
    }
    for record in records:
        bucket = buckets[record["olcek"]]
        bucket["toplam"] += 1
        bucket["uzun_ince"] += int(bool(record.get("uzun_ince")))
        bucket["dusuk_kompaktlik"] += int(bool(record.get("dusuk_kompaktlik")))
    return buckets


def _self_check():
    square = [(r, c) for r in range(4) for c in range(4)]
    long_strip = [(r, c) for r in range(2) for c in range(12)]
    bent = [(0, c) for c in range(8)] + [(r, 7) for r in range(1, 8)]
    bbox = [26.30, 38.20, 26.32, 38.22]
    shape = (20, 20)

    square_record = _shape_record(square, bbox, shape, 100.0)
    strip_record = _shape_record(long_strip, bbox, shape, 100.0)
    bent_record = _shape_record(bent, bbox, shape, 100.0)

    assert not square_record["uzun_ince"], square_record
    assert strip_record["uzun_ince"], strip_record
    assert square_record["kompaktlik"] > bent_record["kompaktlik"], (
        square_record,
        bent_record,
    )
    assert _area_bucket(500) == "kucuk_250_800"
    assert _area_bucket(5000) == "santiye_olcegi_800_10000"
    assert _area_bucket(15000) == "genis_10000_ustu"


def _today_snapshot(connection, report_date, region_key):
    row = connection.execute(
        """SELECT son_item,hata FROM gunluk_uydu_raporlari
        WHERE rapor_tarihi=? AND bolge=? LIMIT 1""",
        (report_date, region_key),
    ).fetchone()
    if not row:
        return None, "rapor_yok"
    return row[0], row[1]


def _analyze_region(region_key, pair):
    captured = {"records": [], "selected": []}

    def audited_hotspots(
        change_mask,
        bbox,
        pixel_area_m2,
        small_site_mask=None,
        limit=satellite.HOTSPOT_LIMIT,
        small_quota=satellite.SMALL_HOTSPOT_QUOTA,
    ):
        records = []
        for component in satellite._connected_components(change_mask):
            if not _eligible(component, pixel_area_m2, small_site_mask):
                continue
            records.append(
                _shape_record(
                    component,
                    bbox,
                    change_mask.shape,
                    pixel_area_m2,
                )
            )
        selected = _ORIGINAL_HOTSPOTS(
            change_mask,
            bbox,
            pixel_area_m2,
            small_site_mask=small_site_mask,
            limit=limit,
            small_quota=small_quota,
        )
        captured["records"] = records
        captured["selected"] = [item for item in selected if isinstance(item, dict)]
        return selected

    original = satellite._hotspots
    satellite._hotspots = audited_hotspots
    try:
        satellite.analyze_sentinel_change(region_key, pair=pair)
    finally:
        satellite._hotspots = original

    selected_keys = {
        key for key in map(_candidate_key, captured["selected"]) if key is not None
    }
    selected_records = [
        record
        for record in captured["records"]
        if _candidate_key(record) in selected_keys
    ]
    flagged_selected_wide = [
        record
        for record in selected_records
        if record["olcek"] == "genis_10000_ustu"
        and (record["uzun_ince"] or record["dusuk_kompaktlik"])
    ]
    examples = sorted(
        flagged_selected_wide,
        key=lambda item: (
            not item["uzun_ince"],
            not item["dusuk_kompaktlik"],
            -item["alan_m2"],
        ),
    )[:EXAMPLE_LIMIT]

    return {
        "ham_gecerli_aday": len(captured["records"]),
        "uretim_secimi": len(captured["selected"]),
        "ham_sekil_dagilimi": _summarize(captured["records"]),
        "uretim_secimi_sekil_dagilimi": _summarize(selected_records),
        "secili_genis_sekil_isaretli": len(flagged_selected_wide),
        "secili_genis_sekil_ornekleri": examples,
    }


def audit_shapes():
    ensure_daily_schema()
    _self_check()
    now = datetime.now(ISTANBUL)
    report_date = now.strftime("%Y-%m-%d")
    regions = {}

    with connect() as connection:
        for region_key in REPORT_REGIONS:
            son_item, hata = _today_snapshot(connection, report_date, region_key)
            record = {
                "bolge": satellite.REGIONS[region_key]["label"],
                "son_item": son_item,
                "durum": "ok",
            }
            if hata:
                record["durum"] = "gunluk_uydu_hatasi"
                record["hata"] = str(hata)
                regions[region_key] = record
                continue
            try:
                pair = satellite.sentinel_pair(region_key)
                latest_item = pair[1].get("id")
                record["latest_item"] = latest_item
                if not son_item or son_item != latest_item:
                    record["durum"] = "gunluk_rapor_latest_ile_eslesmiyor"
                    regions[region_key] = record
                    continue
                record.update(_analyze_region(region_key, pair))
            except Exception as exc:
                record["durum"] = "denetim_hatasi"
                record["hata"] = f"{type(exc).__name__}: {exc}"
            regions[region_key] = record

    payload = {
        "rapor_tarihi": report_date,
        "olusturma": now.strftime("%Y-%m-%d %H:%M %Z"),
        "amac": (
            "Üretim alarmını değiştirmeden 250 m²+ Sentinel kümelerinde uzun-ince ve "
            "düşük-kompaktlık geometrisini ölçmek; özellikle geniş adaylarda yol/altyapı "
            "gibi yanlış-pozitif olasılıklarının saha geri bildirimiyle kalibre edilmesini "
            "sağlamak."
        ),
        "esikler": {
            "uzun_ince_min_uzun_kisa_orani": LONG_THIN_ASPECT_MIN,
            "dusuk_kompaktlik_max": LOW_COMPACTNESS_MAX,
        },
        "uyari": (
            "Şekil etiketi tek başına yol veya yanlış pozitif kanıtı değildir; hiçbir aday "
            "bu denetim nedeniyle elenmez veya öncelik kaybetmez."
        ),
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
            "Şekil yanlış-pozitif denetimi kalite kontrolü başarılı: uzun-ince ve "
            "kompaktlık ölçümleri üretim alarmına dokunmadan hesaplanıyor."
        )
        return

    payload = audit_shapes()
    summaries = []
    for key, item in payload["bolgeler"].items():
        if item.get("durum") != "ok":
            summaries.append(f"{key}: {item.get('durum')}")
            continue
        selected = item.get("uretim_secimi_sekil_dagilimi", {})
        wide = selected.get("genis_10000_ustu", {})
        summaries.append(
            f"{key}: seçili >10000={wide.get('toplam', 0)}; "
            f"uzun-ince={wide.get('uzun_ince', 0)}, "
            f"düşük-kompaktlık={wide.get('dusuk_kompaktlik', 0)}, "
            f"şekil-işaretli geniş={item.get('secili_genis_sekil_isaretli', 0)}"
        )
    print("Şekil yanlış-pozitif denetimi: " + " | ".join(summaries))


if __name__ == "__main__":
    main()
