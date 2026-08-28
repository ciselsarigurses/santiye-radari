"""Günlük saha adaylarında mekânsal tutarlılık ve koordinat kalite kontrolü.

Bu kontrol adayları silmez veya önceliğini değiştirmez. Yanlış koordinat, 250 m²
altı kayıt, bölge dışına taşmış nokta, bozuk rota ya da uydu motoruyla rapor
önceliği arasındaki tutarsızlıkların canlı rapora sessizce girmesini engeller.
Yakın mükerrer ve analiz kutusu sınırına yakın adaylar ise gerçek komşu şantiye
olabileceği için yalnız uyarı olarak raporlanır.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from satellite import MIN_HOTSPOT_AREA_M2, REGIONS, SMALL_HOTSPOT_MAX_M2


REPORT_FILE = Path(__file__).with_name("latest_report.json")
REGION_LABEL_TO_KEY = {
    REGIONS["cesme"]["label"]: "cesme",
    REGIONS["uzunkuyu"]["label"]: "uzunkuyu",
}
DUPLICATE_WARNING_METERS = 80
EDGE_WARNING_METERS = 200


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _inside_bbox(bbox, latitude, longitude):
    west, south, east, north = bbox
    return west <= longitude <= east and south <= latitude <= north


def _edge_distance_m(bbox, latitude, longitude):
    west, south, east, north = bbox
    mean_lat = math.radians(latitude)
    distances = (
        abs(longitude - west) * 111320 * math.cos(mean_lat),
        abs(east - longitude) * 111320 * math.cos(mean_lat),
        abs(latitude - south) * 110570,
        abs(north - latitude) * 110570,
    )
    return min(distances)


def _expected_priority(area_m2, size_class, signal):
    strong_small = (
        str(size_class or "").upper() == "KUCUK"
        or "küçük, güçlü" in str(signal or "").casefold()
    )
    if area_m2 >= 5000:
        return "YÜKSEK"
    if area_m2 >= 2000:
        return "ORTA"
    if strong_small and area_m2 >= MIN_HOTSPOT_AREA_M2:
        return "ORTA"
    return "NORMAL"


def validate_report(payload):
    candidates = payload.get("saha_adaylari") or []
    if not isinstance(candidates, list):
        raise AssertionError("saha_adaylari liste değil.")

    task_ids = set()
    points = []
    warnings = []

    for index, item in enumerate(candidates, start=1):
        if not isinstance(item, dict):
            raise AssertionError(f"Aday #{index} sözlük değil.")

        latitude = _number(item.get("enlem"))
        longitude = _number(item.get("boylam"))
        area_m2 = _number(item.get("alan_m2"))
        if latitude is None or longitude is None:
            raise AssertionError(f"Aday #{index}: koordinat eksik veya geçersiz.")
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise AssertionError(f"Aday #{index}: koordinat dünya sınırları dışında.")
        if area_m2 is None or area_m2 < MIN_HOTSPOT_AREA_M2:
            raise AssertionError(
                f"Aday #{index}: {area_m2} m²; minimum {MIN_HOTSPOT_AREA_M2} m² eşiğini bozuyor."
            )

        region_label = str(item.get("bolge") or "")
        region_key = REGION_LABEL_TO_KEY.get(region_label)
        if region_key is None:
            raise AssertionError(f"Aday #{index}: bilinmeyen uydu bölgesi: {region_label!r}")
        bbox = REGIONS[region_key]["bbox"]
        if not _inside_bbox(bbox, latitude, longitude):
            raise AssertionError(
                f"Aday #{index}: {latitude:.6f},{longitude:.6f} {region_key} analiz kutusunun dışında."
            )

        edge_distance = _edge_distance_m(bbox, latitude, longitude)
        if edge_distance < EDGE_WARNING_METERS:
            warnings.append(
                f"Aday #{index} analiz sınırına yaklaşık {edge_distance:.0f} m yakın; "
                "küme kesilmesi ihtimali sonraki görüntüde kontrol edilmeli."
            )

        size_class = str(item.get("boyut_sinifi") or "").upper()
        signal = str(item.get("sinyal") or "")
        if size_class == "KUCUK":
            if not (MIN_HOTSPOT_AREA_M2 <= area_m2 < SMALL_HOTSPOT_MAX_M2):
                raise AssertionError(
                    f"Aday #{index}: KUCUK sınıfı {area_m2:.0f} m² ile beklenen aralık dışında."
                )
            if "küçük, güçlü" not in signal.casefold():
                raise AssertionError(
                    f"Aday #{index}: KUCUK sınıfı güçlü küçük-saha sinyali taşımıyor."
                )

        expected_priority = _expected_priority(area_m2, size_class, signal)
        actual_priority = str(item.get("oncelik") or "")
        if actual_priority != expected_priority:
            raise AssertionError(
                f"Aday #{index}: öncelik {actual_priority!r}, beklenen {expected_priority!r}."
            )

        route = str(item.get("harita") or "")
        coordinate_token = f"{latitude:.6f},{longitude:.6f}"
        if coordinate_token not in route:
            raise AssertionError(
                f"Aday #{index}: Google Maps rotası rapor koordinatıyla eşleşmiyor."
            )

        task_id = str(item.get("gorev_id") or "").strip()
        if task_id:
            if task_id in task_ids:
                raise AssertionError(f"Aynı görev kimliği raporda iki kez var: {task_id}")
            task_ids.add(task_id)

        points.append((index, region_key, latitude, longitude))

    for position, first in enumerate(points):
        for second in points[position + 1:]:
            if first[1] != second[1]:
                continue
            distance = _distance_m((first[2], first[3]), (second[2], second[3]))
            if distance < DUPLICATE_WARNING_METERS:
                warnings.append(
                    f"Aday #{first[0]} ile #{second[0]} yalnızca {distance:.0f} m uzakta; "
                    "iki ayrı saha mı mükerrer mi saha kontrolünde teyit edilmeli."
                )

    return len(candidates), warnings


def main():
    if not REPORT_FILE.exists():
        raise SystemExit("latest_report.json bulunamadı; mekânsal kalite kontrolü çalıştırılamadı.")
    payload = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    count, warnings = validate_report(payload)
    print(f"Mekânsal kalite kontrolü başarılı: {count} saha adayı doğrulandı.")
    if warnings:
        print("Dikkat uyarıları:")
        for warning in warnings:
            print(f"- {warning}")
    else:
        print("Yakın mükerrer veya analiz sınırına aşırı yakın aday görülmedi.")


if __name__ == "__main__":
    main()
