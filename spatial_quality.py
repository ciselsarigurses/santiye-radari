"""Günlük saha adaylarında mekânsal tutarlılık ve koordinat kalite kontrolü.

Bu kontrol adayları silmez veya önceliğini değiştirmez. Yanlış koordinat, 250 m²
altı kayıt, bölge dışına taşmış nokta, bozuk rota ya da uydu motoruyla rapor
önceliği arasındaki tutarsızlıkların canlı rapora sessizce girmesini engeller.
Yakın mükerrer ve toplam analiz kapsamasının dış sınırına yakın adaylar ise gerçek
komşu şantiye veya küme kesilmesi riski olabileceği için yalnız uyarı olarak
raporlanır. Bir bölgenin iç sınırı diğer analiz kutusuyla güvenli biçimde örtüşüyorsa
kör alan sayılmaz.
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
ANALYZED_REGION_KEYS = tuple(REGION_LABEL_TO_KEY.values())
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


def _coverage_edge_distance_m(latitude, longitude):
    """Noktanın fiilen analiz edilen kutuların birleşik kapsamasındaki kenar payı.

    Çeşme ve Uzunkuyu kutuları 26.45–26.53 E bandında bilinçli olarak örtüşür.
    Bir aday Uzunkuyu kutusunun batı kenarına 25 m yakın görünse bile Çeşme
    kutusunun binlerce metre içinde olabilir. Böyle bir iç örtüşme sınırını kör
    alan diye raporlamak yanlış alarm üretir. Noktayı kapsayan analiz kutuları
    arasındaki en büyük kenar payını kullanarak yalnız gerçek dış kapsama kenarını
    uyarırız.
    """
    margins = []
    for region_key in ANALYZED_REGION_KEYS:
        bbox = REGIONS[region_key]["bbox"]
        if _inside_bbox(bbox, latitude, longitude):
            margins.append(_edge_distance_m(bbox, latitude, longitude))
    return max(margins) if margins else 0.0


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


def _self_test_coverage_edges():
    # Canlı raporda görülen örneğe benzer biçimde Uzunkuyu'nun batı iç sınırına
    # ~25 m yakın bir nokta Çeşme kutusunun güvenli içindedir; kör alan değildir.
    overlap_point = (38.232526, 26.450286)
    source_margin = _edge_distance_m(REGIONS["uzunkuyu"]["bbox"], *overlap_point)
    coverage_margin = _coverage_edge_distance_m(*overlap_point)
    assert source_margin < 50, "İç örtüşme sınırı test noktası beklenen kenara yakın değil."
    assert coverage_margin > EDGE_WARNING_METERS, (
        "Diğer analiz kutusunun sağladığı kapsama payı iç sınırı kurtarmadı."
    )

    # Güney dış sınırı her iki kutuda da aynıdır; burada gerçekten küme kesilmesi
    # riski vardır ve birleşik kapsama hesabı bunu gizlememelidir.
    outer_edge_point = (38.180500, 26.300000)
    assert _coverage_edge_distance_m(*outer_edge_point) < EDGE_WARNING_METERS, (
        "Gerçek dış kapsama kenarı yanlışlıkla güvenli sayıldı."
    )


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

        edge_distance = _coverage_edge_distance_m(latitude, longitude)
        if edge_distance < EDGE_WARNING_METERS:
            warnings.append(
                f"Aday #{index} toplam analiz kapsamasının dış sınırına yaklaşık "
                f"{edge_distance:.0f} m yakın; küme kesilmesi ihtimali sonraki "
                "görüntüde kontrol edilmeli."
            )

        size_class = str(item.get("boyut_sinifi") or "").upper()
        signal = str(item.get("sinyal") or "")
        if size_class == "KUCUK":
            # Uydu motoru sınıfı yuvarlama öncesi gerçek alana göre (<800 m²)
            # belirler, rapor ise alanı tam m²'ye yuvarlar. 799.x m² bu nedenle
            # raporda 800 m² görünebilir; sınıf doğru olduğu halde kalite kontrolü
            # bunu hata saymamalı.
            if not (MIN_HOTSPOT_AREA_M2 <= area_m2 <= SMALL_HOTSPOT_MAX_M2):
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
    _self_test_coverage_edges()
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
        print("Yakın mükerrer veya toplam analiz kapsamasının dış sınırına aşırı yakın aday görülmedi.")


if __name__ == "__main__":
    main()
