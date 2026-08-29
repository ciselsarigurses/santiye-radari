"""Çakışan Sentinel analiz kutularının aynı sahayı iki kez göreve çevirmesini önler.

Çeşme ve Uzunkuyu kutuları kapsama körlüğü bırakmamak için bilinçli olarak örtüşür.
Aynı Sentinel değişim kümesi bu ortak şeritte iki bölgede de üretilebilir. Bu dosya
yalnız çok sıkı bir eşleşmede (aynı görüntü aralığı, aynı boyut sınıfı, benzer alan
ve en fazla 25 m merkez farkı) bugünün ``hareket_json`` kayıtlarından mükerrer olanı
çıkarır. Gerçek komşu şantiyeleri birleştirmemek için eşik 80 m saha-görev eşleştirme
mesafesinden belirgin biçimde daha sıkıdır.
"""

from __future__ import annotations

import json
import math
from datetime import datetime

from daily_report import ISTANBUL, _report_hotspots, _write_public_report
from satellite import REGIONS, SMALL_HOTSPOT_MAX_M2
from scanner import connect


OVERLAP_DUPLICATE_METERS = 25
MIN_AREA_SIMILARITY = 0.70


def _number(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _edge_distance_m(region_key, latitude, longitude):
    bbox = REGIONS.get(region_key, {}).get("bbox")
    if not bbox:
        return 0.0
    west, south, east, north = bbox
    mean_lat = math.radians(latitude)
    return min(
        abs(longitude - west) * 111320 * math.cos(mean_lat),
        abs(east - longitude) * 111320 * math.cos(mean_lat),
        abs(latitude - south) * 110570,
        abs(north - latitude) * 110570,
    )


def _size_class(item):
    value = str(item.get("boyut_sinifi") or "").strip().upper()
    if value:
        return value
    area = _number(item.get("alan_m2"), 0) or 0
    return "KUCUK" if area < SMALL_HOTSPOT_MAX_M2 else "STANDART"


def _area_similarity(first, second):
    first_area = _number(first.get("alan_m2"), 0) or 0
    second_area = _number(second.get("alan_m2"), 0) or 0
    larger = max(first_area, second_area)
    if larger <= 0:
        return 0.0
    return min(first_area, second_area) / larger


def _is_overlap_duplicate(first, second):
    if first["region_key"] == second["region_key"]:
        return False
    if first["older_date"] != second["older_date"]:
        return False
    if first["latest_date"] != second["latest_date"]:
        return False
    if _size_class(first["item"]) != _size_class(second["item"]):
        return False
    if _area_similarity(first["item"], second["item"]) < MIN_AREA_SIMILARITY:
        return False
    return _distance_m(first["point"], second["point"]) <= OVERLAP_DUPLICATE_METERS


def _preferred(first, second):
    """Sınırdan daha uzaktaki ölçümü, eşitse daha geniş kümeyi koru."""
    first_edge = _edge_distance_m(first["region_key"], *first["point"])
    second_edge = _edge_distance_m(second["region_key"], *second["point"])
    if abs(first_edge - second_edge) > 1:
        return first if first_edge > second_edge else second
    first_area = _number(first["item"].get("alan_m2"), 0) or 0
    second_area = _number(second["item"].get("alan_m2"), 0) or 0
    return first if first_area >= second_area else second


def _self_test():
    base = {
        "older_date": "24.08.2026",
        "latest_date": "26.08.2026",
    }
    first = {
        **base,
        "region_key": "cesme",
        "point": (38.320000, 26.490000),
        "item": {"alan_m2": 1000, "boyut_sinifi": "STANDART"},
    }
    second = {
        **base,
        "region_key": "uzunkuyu",
        "point": (38.320090, 26.490090),
        "item": {"alan_m2": 950, "boyut_sinifi": "STANDART"},
    }
    assert _is_overlap_duplicate(first, second), "Yakın ortak-kutu mükerreri tanınmadı."

    far = dict(second)
    far["point"] = (38.320500, 26.490500)
    assert not _is_overlap_duplicate(first, far), "Uzak iki gerçek saha yanlış birleştirildi."

    different_area = dict(second)
    different_area["item"] = {"alan_m2": 400, "boyut_sinifi": "STANDART"}
    assert not _is_overlap_duplicate(
        first, different_area
    ), "Alanı belirgin farklı iki saha yanlış birleştirildi."


def dedupe_today(report_date=None):
    report_date = report_date or datetime.now(ISTANBUL).strftime("%Y-%m-%d")
    removed = []
    raw_summary = None
    created = None
    details = []

    with connect() as connection:
        rows = connection.execute(
            """SELECT bolge,bolge_adi,onceki_tarih,son_tarih,hareket_json,hata
            FROM gunluk_uydu_raporlari
            WHERE rapor_tarihi=? ORDER BY bolge""",
            (report_date,),
        ).fetchall()
        region_movements = {}
        flattened = []
        for region_key, region_name, older_date, latest_date, movement_json, error in rows:
            if error:
                continue
            try:
                movement = json.loads(movement_json or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                movement = []
            if not isinstance(movement, list):
                continue
            region_key = str(region_key or "")
            region_movements[region_key] = movement
            for index, item in enumerate(movement):
                if not isinstance(item, dict):
                    continue
                latitude = _number(item.get("enlem"))
                longitude = _number(item.get("boylam"))
                if latitude is None or longitude is None:
                    continue
                flattened.append(
                    {
                        "region_key": region_key,
                        "region_name": str(region_name or region_key),
                        "older_date": older_date,
                        "latest_date": latest_date,
                        "index": index,
                        "point": (latitude, longitude),
                        "item": item,
                    }
                )

        dropped = set()
        for position, first in enumerate(flattened):
            first_key = (first["region_key"], first["index"])
            if first_key in dropped:
                continue
            for second in flattened[position + 1:]:
                second_key = (second["region_key"], second["index"])
                if second_key in dropped or not _is_overlap_duplicate(first, second):
                    continue
                keep = _preferred(first, second)
                lose = second if keep is first else first
                lose_key = (lose["region_key"], lose["index"])
                dropped.add(lose_key)
                removed.append(
                    {
                        "korunan_bolge": keep["region_name"],
                        "elenen_bolge": lose["region_name"],
                        "mesafe_m": round(_distance_m(first["point"], second["point"]), 1),
                        "korunan_alan_m2": round(_number(keep["item"].get("alan_m2"), 0) or 0),
                        "elenen_alan_m2": round(_number(lose["item"].get("alan_m2"), 0) or 0),
                    }
                )
                if lose is first:
                    break

        if dropped:
            for region_key, movement in region_movements.items():
                filtered = [
                    item for index, item in enumerate(movement)
                    if (region_key, index) not in dropped
                ]
                if len(filtered) == len(movement):
                    continue
                connection.execute(
                    """UPDATE gunluk_uydu_raporlari SET hareket_json=?
                    WHERE rapor_tarihi=? AND bolge=?""",
                    (json.dumps(filtered, ensure_ascii=False), report_date, region_key),
                )

            report_row = connection.execute(
                """SELECT olusturma,ozet,internet_detay_json
                FROM gunluk_raporlar WHERE rapor_tarihi=?""",
                (report_date,),
            ).fetchone()
            if report_row:
                created = report_row[0]
                raw_summary = report_row[1]
                try:
                    details = json.loads(report_row[2] or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    details = []
                if not isinstance(details, list):
                    details = []

        # Güncel rapor adaylarını aynı transaction içinde, elenmiş hareketleri
        # dikkate alarak oluştur; dosyaya yazmayı commit sonrasına bırak.
        hotspots = _report_hotspots(connection, report_date) if dropped else None

    if dropped and created and raw_summary is not None:
        _write_public_report(report_date, created, raw_summary, hotspots or [], details)

    return removed


def main():
    _self_test()
    removed = dedupe_today()
    if not removed:
        print(
            "Çakışan Çeşme/Uzunkuyu kutularında 25 m ve sıkı alan-benzerliği "
            "eşiğini geçen mükerrer uydu adayı yok."
        )
        return
    print(f"Ortak Sentinel kapsamasında {len(removed)} mükerrer saha adayı elendi.")
    for item in removed:
        print(
            "- "
            f"{item['elenen_bolge']} → {item['korunan_bolge']} · "
            f"{item['mesafe_m']} m · "
            f"{item['elenen_alan_m2']}→{item['korunan_alan_m2']} m²"
        )


if __name__ == "__main__":
    main()
