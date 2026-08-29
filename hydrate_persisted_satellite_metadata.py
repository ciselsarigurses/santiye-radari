"""Açık fakat son analiz kümesinde görünmeyen uydu görevlerinin son ölçüsünü korur.

Bir bbox/çözünürlük/algoritma yeniden analizi aynı Sentinel görüntüsünü biraz farklı
pikselleştirebilir. Saha görevi güvenlik nedeniyle açık tutulurken ``report_quality``
bu kaydı 0 m² genel bir BEKLEYEN görev olarak gösterir. Bu yardımcı yalnızca açık
uydu görevlerinde, aynı bölgede ve en fazla 25 m uzakta bulunan son tarihsel uydu
adayının alan/sinyal/görüntü aralığını rapora geri taşır. Görevi otomatik olarak
kapatmaz, yeni aday üretmez ve koordinatı değiştirmez.

Açık bir güncel hotspot'u mevcut göreve bağlarken 80 m Sentinel centroid toleransı
uygundur; ancak eski bir alan/sinyal ölçüsünü göreve geri kopyalamak daha güçlü bir
iddiadır. Bu nedenle tarihsel metadata eşleşmesi 25 m ile sınırlıdır. Böylece yakın
iki ayrı parselden komşu şantiyenin eski ölçüsünü yanlış göreve taşıma riski azalır.
"""

from __future__ import annotations

import json
import math

from daily_report import (
    LATEST_REPORT_JSON,
    _field_priority,
    _priority_reason,
    _write_public_report,
)
from scanner import connect


MATCH_METERS = 25
HISTORY_ROWS = 30


def _distance_m(lat1, lon1, lat2, lon2):
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _movement_list(raw):
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _number(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _historical_match(connection, item, report_date):
    region = str(item.get("bolge") or "").strip()
    latitude = _number(item.get("enlem"))
    longitude = _number(item.get("boylam"))
    if not region or latitude is None or longitude is None:
        return None

    rows = connection.execute(
        """SELECT rapor_tarihi,onceki_tarih,son_tarih,hareket_json
        FROM gunluk_uydu_raporlari
        WHERE rapor_tarihi<=? AND hata IS NULL
          AND hareket_json IS NOT NULL
          AND (bolge_adi=? OR bolge=?)
        ORDER BY rapor_tarihi DESC, id DESC LIMIT ?""",
        (report_date, region, region, HISTORY_ROWS),
    ).fetchall()

    for _row_date, older_date, latest_date, movement_json in rows:
        nearest = None
        nearest_distance = None
        for candidate in _movement_list(movement_json):
            if not isinstance(candidate, dict):
                continue
            cand_lat = _number(candidate.get("enlem"))
            cand_lon = _number(candidate.get("boylam"))
            area_m2 = _number(candidate.get("alan_m2"), 0)
            if cand_lat is None or cand_lon is None or not area_m2 or area_m2 <= 0:
                continue
            distance = _distance_m(latitude, longitude, cand_lat, cand_lon)
            if distance > MATCH_METERS:
                continue
            if nearest_distance is None or distance < nearest_distance:
                nearest = candidate
                nearest_distance = distance
        if nearest is not None:
            return {
                "alan_m2": round(_number(nearest.get("alan_m2"), 0)),
                "sinyal": str(nearest.get("sinyal") or "Uydu yüzey değişimi adayı"),
                "boyut_sinifi": str(nearest.get("boyut_sinifi") or "") or None,
                "onceki_tarih": older_date,
                "son_tarih": latest_date,
                "mesafe_m": round(nearest_distance, 1),
            }
    return None


def hydrate_report():
    try:
        payload = json.loads(LATEST_REPORT_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0

    report_date = str(payload.get("rapor_tarihi") or "")[:10]
    created = str(payload.get("olusturma") or "")
    summary = str(payload.get("ozet") or "")
    details = payload.get("yeni_internet_bulgulari") or []
    hotspots = payload.get("saha_adaylari") or []
    if not report_date or not isinstance(hotspots, list):
        return 0

    hydrated = 0
    with connect() as connection:
        for item in hotspots:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("gorev_id") or "")
            area_m2 = _number(item.get("alan_m2"), 0) or 0
            # Saha kayıtlarına veya zaten ölçüsü bulunan güncel uydu adaylarına dokunma.
            if not task_id.startswith("U") or area_m2 > 0:
                continue

            historical = _historical_match(connection, item, report_date)
            if not historical:
                continue

            hist_area = int(historical["alan_m2"])
            hist_signal = historical["sinyal"]
            size_class = historical.get("boyut_sinifi") or ""
            status = str(item.get("saha_durumu") or "KONTROLE_GIT")
            waiting_days = int(_number(item.get("bekleme_gun"), 0) or 0)

            if status == "TEKRAR_GIT":
                prefix = "Tekrar saha kontrolü · "
            elif bool(item.get("gecikmis")):
                prefix = f"{waiting_days} gündür saha kontrolü bekliyor · "
            else:
                prefix = "Son yeniden analizde tekrar görünmedi · "

            item["alan_m2"] = hist_area
            item["sinyal"] = prefix + hist_signal
            item["boyut_sinifi"] = size_class or None
            item["onceki_tarih"] = historical.get("onceki_tarih")
            item["son_tarih"] = historical.get("son_tarih")
            item["tarihsel_esleme_mesafe_m"] = historical.get("mesafe_m")
            item["uydu_onceligi"] = _field_priority(hist_area, size_class, hist_signal)
            item["oncelik_nedeni"] = (
                "Son analiz kümesinde tekrar görünmedi; saha görevi açık kaldığı için "
                "son güvenilir uydu ölçüsü korunuyor. "
                + _priority_reason(hist_area, size_class, hist_signal)
            )
            item["konum_notu"] = (
                "Koordinat açık saha görevinin yaklaşık merkezidir. Alan ve sinyal son "
                "eşleşen uydu adayından en fazla 25 m tarihsel eşleşmeyle taşınmıştır; "
                "son yeniden analizde küme tekrar görünmediği için saha teyidi "
                "beklenmektedir."
            )
            hydrated += 1

    if hydrated:
        _write_public_report(report_date, created, summary, hotspots, details)
    return hydrated


if __name__ == "__main__":
    count = hydrate_report()
    if count:
        print(f"Açık uydu görevlerinde {count} tarihsel aday ölçüsü rapora geri taşındı.")
    else:
        print("Tarihsel uydu ölçüsü gerektiren açık görev yok.")
