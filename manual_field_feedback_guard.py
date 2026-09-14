"""Görev kimliği olmayan güvenilir saha geri bildirimini MİKRO kısa listeye uygular.

Bu koruma özellikle sahada doğrulanmış belediye/peyzaj temizliği gibi 150-249 m²
yanlış pozitifleri kalıcı kara listeye çevirmeden işler. Aynı veya daha eski
Sentinel sahnesi aynı noktayı yeniden üretirse aktif diagnostik kısa listeden
çıkarılır ve çıktı içinde arka-plan kaydı olarak tutulur. Geri bildirim tarihinden
daha yeni uydu kanıtı ise burada bastırılmaz; normal temporal/kompaktlık/ek kanıt
kapılarından yeniden değerlendirilebilir.

Ana 250 m² üretim eşiğine, alarma veya saha görevi üretimine dokunmaz.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np


BASE_DIR = Path(__file__).resolve().parent
SHORTLIST_FILE = BASE_DIR / "micro_site_shortlist.json"
REGISTRY_FILE = BASE_DIR / "manual_field_feedback.json"
SUPPORTED_OUTCOMES = {"YANLIS_POZITIF", "TARIMSAL", "DOGAL_DEGISIM"}
MICRO_MIN_M2 = 150
MICRO_MAX_M2 = 249
DEFAULT_RADIUS_M = 25.0


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
            pass
    return None


def _distance_m(first: dict[str, Any], second: dict[str, Any]) -> float:
    lat1 = _number(first.get("enlem"))
    lon1 = _number(first.get("boylam"))
    lat2 = _number(second.get("enlem"))
    lon2 = _number(second.get("boylam"))
    if None in (lat1, lon1, lat2, lon2):
        return float("inf")
    mean_lat = (lat1 + lat2) / 2.0
    north_m = (lat2 - lat1) * 110570.0
    east_m = (lon2 - lon1) * 111320.0 * np.cos(np.radians(mean_lat))
    return float(np.hypot(north_m, east_m))


def _candidate_source_date(candidate: dict[str, Any]) -> date | None:
    for key in (
        "mikro_kaynak_sentinel_tarihi",
        "son_tarih",
        "kaynak_sentinel_tarihi",
    ):
        parsed = _parse_date(candidate.get(key))
        if parsed is not None:
            return parsed
    return None


def _load_registry() -> list[dict[str, Any]]:
    if not REGISTRY_FILE.exists():
        return []
    payload = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    records = payload.get("kayitlar") if isinstance(payload, dict) else []
    return [item for item in (records or []) if isinstance(item, dict)]


def _matching_feedback(
    candidate: dict[str, Any], records: list[dict[str, Any]]
) -> tuple[dict[str, Any], float] | None:
    area = _number(candidate.get("alan_m2"), 0.0) or 0.0
    if not MICRO_MIN_M2 <= area <= MICRO_MAX_M2:
        return None

    candidate_date = _candidate_source_date(candidate)
    for record in records:
        outcome = str(record.get("sonuc") or "").strip().upper()
        if outcome not in SUPPORTED_OUTCOMES:
            continue
        feedback_date = _parse_date(record.get("sonuc_tarihi"))
        # Yeni bir Sentinel sahnesi kalıcı olarak bastırılmaz. Tarih bilinmiyorsa
        # kayıt daha yeni olduğu ispatlanamadığından eski/same-scene tarafında kalır.
        if feedback_date is not None and candidate_date is not None:
            if candidate_date > feedback_date:
                continue
        radius = _number(record.get("eslesme_yaricapi_m"), DEFAULT_RADIUS_M)
        radius = max(1.0, min(float(radius or DEFAULT_RADIUS_M), 50.0))
        distance = _distance_m(candidate, record)
        if distance <= radius:
            return record, distance
    return None


def apply_guard(
    payload: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    result = dict(payload)
    shortlist = [
        dict(item) for item in (payload.get("kisa_liste") or []) if isinstance(item, dict)
    ]
    kept: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []

    for candidate in shortlist:
        match = _matching_feedback(candidate, records)
        if match is None:
            kept.append(candidate)
            continue
        record, distance = match
        row = dict(candidate)
        row["saha_geri_bildirim_bastirma"] = True
        row["saha_geri_bildirim_id"] = record.get("id")
        row["saha_geri_bildirim_sonuc"] = record.get("sonuc")
        row["saha_geri_bildirim_neden"] = record.get("neden")
        row["saha_geri_bildirim_sonuc_tarihi"] = record.get("sonuc_tarihi")
        row["saha_geri_bildirim_mesafe_m"] = round(distance, 1)
        row["yeniden_degerlendirme"] = (
            "Sonuç tarihinden daha yeni Sentinel kanıtında normal güçlü-kanıt "
            "kapılarından yeniden değerlendir."
        )
        suppressed.append(row)

    result["kisa_liste"] = kept
    previous_background = [
        dict(item)
        for item in (payload.get("saha_geri_bildirim_arka_plan") or [])
        if isinstance(item, dict)
    ]
    # Aynı kaydı her çalışmada çoğaltma; güncel bastırma snapshot'ı tek kaynak olsun.
    by_key: dict[tuple[Any, Any, Any, Any], dict[str, Any]] = {}
    for item in [*previous_background, *suppressed]:
        key = (
            item.get("saha_geri_bildirim_id"),
            item.get("enlem"),
            item.get("boylam"),
            item.get("mikro_kaynak_sentinel_tarihi") or item.get("son_tarih"),
        )
        by_key[key] = item
    result["saha_geri_bildirim_arka_plan"] = list(by_key.values())
    result["saha_geri_bildirim_bastirilan"] = len(suppressed)
    result["saha_geri_bildirim_kayit_sayisi"] = len(records)
    result["alarm"] = False
    result["saha_gorevi"] = False
    result["ana_uretim_esigi_m2"] = payload.get("ana_uretim_esigi_m2", 250)
    result["mikro_aralik_m2"] = payload.get("mikro_aralik_m2", [150, 249])
    result["saha_geri_bildirim_notu"] = (
        "Görev kimliği olmayan doğrulanmış saha yanlış-pozitifleri aynı veya daha "
        "eski Sentinel kanıtında aktif MİKRO kısa listeden çıkarılır fakat arka "
        "planda tutulur. Daha yeni uydu kanıtı kalıcı kara listeye alınmaz."
    )
    return result


def _self_check() -> None:
    records = [
        {
            "id": "TEST-FP",
            "sonuc_tarihi": "2026-09-13",
            "enlem": 38.287679,
            "boylam": 26.266305,
            "eslesme_yaricapi_m": 25,
            "sonuc": "YANLIS_POZITIF",
            "neden": "test",
        }
    ]

    def candidate(day: str, lat: float = 38.287679) -> dict[str, Any]:
        return {
            "enlem": lat,
            "boylam": 26.266305,
            "alan_m2": 200,
            "mikro_kaynak_sentinel_tarihi": day,
        }

    base = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "kisa_liste": [candidate("13.09.2026")],
    }
    same_day = apply_guard(base, records)
    assert same_day["kisa_liste"] == []
    assert same_day["saha_geri_bildirim_bastirilan"] == 1
    assert len(same_day["saha_geri_bildirim_arka_plan"]) == 1
    assert same_day["ana_uretim_esigi_m2"] == 250
    assert same_day["mikro_aralik_m2"] == [150, 249]
    assert same_day["alarm"] is False and same_day["saha_gorevi"] is False

    older = dict(base, kisa_liste=[candidate("08.09.2026")])
    assert apply_guard(older, records)["kisa_liste"] == []

    newer = dict(base, kisa_liste=[candidate("15.09.2026")])
    assert len(apply_guard(newer, records)["kisa_liste"]) == 1, (
        "Yeni Sentinel sahnesi saha yanlış-pozitifi nedeniyle kalıcı bastırılamaz."
    )

    far = dict(base, kisa_liste=[candidate("13.09.2026", lat=38.288679)])
    assert len(apply_guard(far, records)["kisa_liste"]) == 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Manuel saha geri-bildirim koruması öz testi başarılı.")
        return
    if not SHORTLIST_FILE.exists():
        raise RuntimeError("micro_site_shortlist.json bulunamadı.")
    payload = json.loads(SHORTLIST_FILE.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("micro_site_shortlist.json nesne olmalıdır.")
    records = _load_registry()
    result = apply_guard(payload, records)
    SHORTLIST_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "Saha geri-bildirim koruması: "
        f"kayıt={len(records)}, bastırılan={result['saha_geri_bildirim_bastirilan']}, "
        f"aktif_mikro={len(result.get('kisa_liste') or [])}. "
        "Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
