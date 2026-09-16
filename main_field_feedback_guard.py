"""Doğrulanmış saha yanlış-pozitiflerini ana günlük rotadan güvenle çıkarır.

Bu katman alarm/görev silmez ve 250 m² ana üretim eşiğini değiştirmez. Yalnız
``manual_field_feedback.json`` içindeki güvenilir negatif saha sonuçlarını, aynı
veya daha eski Sentinel kanıtıyla eşleşen ana saha adaylarına uygular ve
``rota_uygun=False`` işaretler. Daha yeni Sentinel sahnesi kalıcı olarak
bastırılmaz; normal güçlü-kanıt kapılarından yeniden değerlendirilebilir.

Pozitif ``DOGRULANMIS_KAZI`` kayıtları bu guard tarafından otomatik alarm/göreve
çevrilmez; yalnız kalibrasyon kaydı olarak registry'de tutulur.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parent
REPORT_FILE = BASE / "latest_report.json"
REGISTRY_FILE = BASE / "manual_field_feedback.json"
NEGATIVE_OUTCOMES = {"YANLIS_POZITIF", "TARIMSAL", "DOGAL_DEGISIM"}
DEFAULT_RADIUS_M = 25.0
MAX_RADIUS_M = 50.0
MAIN_MIN_M2 = 250.0


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _candidate_date(item: dict[str, Any]) -> date | None:
    for key in ("son_tarih", "kaynak_sentinel_tarihi", "mikro_kaynak_sentinel_tarihi"):
        parsed = _parse_date(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _distance_m(first: dict[str, Any], second: dict[str, Any]) -> float:
    lat1 = _number(first.get("enlem"))
    lon1 = _number(first.get("boylam"))
    lat2 = _number(second.get("enlem"))
    lon2 = _number(second.get("boylam"))
    if None in (lat1, lon1, lat2, lon2):
        return float("inf")
    mean_lat = math.radians((float(lat1) + float(lat2)) / 2.0)
    north = (float(lat2) - float(lat1)) * 110570.0
    east = (float(lon2) - float(lon1)) * 111320.0 * math.cos(mean_lat)
    return math.hypot(north, east)


def _eligible_feedback(record: dict[str, Any]) -> bool:
    return str(record.get("sonuc") or "").strip().upper() in NEGATIVE_OUTCOMES


def _match(candidate: dict[str, Any], records: list[dict[str, Any]]):
    area = _number(candidate.get("alan_m2"), 0.0) or 0.0
    if area < MAIN_MIN_M2:
        return None
    if str(candidate.get("saha_durumu") or "").strip().upper() == "TEKRAR_GIT":
        return None

    candidate_day = _candidate_date(candidate)
    for record in records:
        if not _eligible_feedback(record):
            continue
        feedback_day = _parse_date(record.get("sonuc_tarihi"))
        # Daha yeni Sentinel kanıtı eski saha kararınca kalıcı bastırılamaz.
        if candidate_day is not None and feedback_day is not None and candidate_day > feedback_day:
            continue
        radius = _number(record.get("eslesme_yaricapi_m"), DEFAULT_RADIUS_M) or DEFAULT_RADIUS_M
        radius = max(1.0, min(float(radius), MAX_RADIUS_M))
        distance = _distance_m(candidate, record)
        if distance <= radius:
            return record, distance
    return None


def apply_guard(report: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    result = dict(report)
    candidates = []
    suppressed = []
    for raw in report.get("saha_adaylari") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        match = _match(item, records)
        if match is not None:
            record, distance = match
            item["rota_uygun"] = False
            item["saha_geri_bildirim_ana_bastirma"] = True
            item["saha_geri_bildirim_id"] = record.get("id")
            item["saha_geri_bildirim_sonuc"] = record.get("sonuc")
            item["saha_geri_bildirim_neden"] = record.get("neden")
            item["saha_geri_bildirim_sonuc_tarihi"] = record.get("sonuc_tarihi")
            item["saha_geri_bildirim_mesafe_m"] = round(distance, 1)
            item["yeniden_degerlendirme"] = (
                "Bu saha sonucu yalnız aynı/eski uydu kanıtını günlük rotadan çıkarır; "
                "daha yeni Sentinel/SAR müdahalesi normal kanıt kapılarından yeniden değerlendirilebilir."
            )
            suppressed.append(
                {
                    "gorev_id": item.get("gorev_id"),
                    "enlem": item.get("enlem"),
                    "boylam": item.get("boylam"),
                    "alan_m2": item.get("alan_m2"),
                    "son_tarih": item.get("son_tarih"),
                    "saha_geri_bildirim_id": record.get("id"),
                    "sonuc": record.get("sonuc"),
                    "mesafe_m": round(distance, 1),
                }
            )
        candidates.append(item)

    result["saha_adaylari"] = candidates
    result["ana_saha_geri_bildirim_bastirilan"] = suppressed
    result["ana_saha_geri_bildirim_bastirilan_sayi"] = len(suppressed)
    result["ana_saha_geri_bildirim_notu"] = (
        "Doğrulanmış negatif saha sonuçları aynı/eski Sentinel kanıtında yalnız günlük rota "
        "uygunluğunu kapatır; görev/alarm silinmez ve daha yeni uydu kanıtı engellenmez."
    )
    return result


def _load_records() -> list[dict[str, Any]]:
    try:
        payload = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records = payload.get("kayitlar") if isinstance(payload, dict) else []
    return [row for row in (records or []) if isinstance(row, dict)]


def _self_check() -> None:
    records = [
        {
            "id": "FP-TEST",
            "sonuc_tarihi": "2026-09-16",
            "enlem": 38.322140,
            "boylam": 26.407912,
            "yaklasik_alan_m2": 500,
            "eslesme_yaricapi_m": 25,
            "sonuc": "YANLIS_POZITIF",
            "neden": "test",
        },
        {
            "id": "TP-TEST",
            "sonuc_tarihi": "2026-09-16",
            "enlem": 38.341846,
            "boylam": 26.432861,
            "sonuc": "DOGRULANMIS_KAZI",
        },
    ]
    base = {
        "gorev_id": "A",
        "enlem": 38.322140,
        "boylam": 26.407912,
        "alan_m2": 500,
        "son_tarih": "15.09.2026",
        "saha_durumu": "KONTROLE_GIT",
        "rota_uygun": True,
    }
    guarded = apply_guard({"saha_adaylari": [base]}, records)
    assert guarded["saha_adaylari"][0]["rota_uygun"] is False
    assert guarded["ana_saha_geri_bildirim_bastirilan_sayi"] == 1

    newer = dict(base, son_tarih="17.09.2026")
    assert apply_guard({"saha_adaylari": [newer]}, records)["saha_adaylari"][0]["rota_uygun"] is True

    repeat = dict(base, saha_durumu="TEKRAR_GIT")
    assert apply_guard({"saha_adaylari": [repeat]}, records)["saha_adaylari"][0]["rota_uygun"] is True

    micro = dict(base, alan_m2=200)
    assert apply_guard({"saha_adaylari": [micro]}, records)["saha_adaylari"][0]["rota_uygun"] is True

    positive = dict(base, enlem=38.341846, boylam=26.432861)
    assert apply_guard({"saha_adaylari": [positive]}, records)["saha_adaylari"][0]["rota_uygun"] is True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.self_check:
        print("Ana saha geri-bildirim koruması öz testi başarılı.")
        return

    if not REPORT_FILE.exists():
        raise RuntimeError("latest_report.json bulunamadı.")
    report = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise RuntimeError("latest_report.json nesne olmalıdır.")
    result = apply_guard(report, _load_records())
    REPORT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "Ana saha geri-bildirim koruması: "
        f"bastırılan={result['ana_saha_geri_bildirim_bastirilan_sayi']}; "
        "alarm/görev silinmedi."
    )


if __name__ == "__main__":
    main()
