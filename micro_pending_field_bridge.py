"""Manuel olarak sahaya gönderilen MİKRO adayları tekrar-dispatch'ten korur.

Bu katman 150-249 m² MİKRO politikasını değiştirmez, alarm veya kalıcı saha görevi
üretmez. Kullanıcının zaten saha kontrolü için personel gönderdiği bir MİKRO adayını
aynı Sentinel sahnesinde yeniden "yeni inceleme adayı" gibi göstermemek için yalnız
bekleyen-kontrol durumuna taşır. Daha yeni Sentinel sahnesi gelirse eski bekleyen
kayıt otomatik veto olmaz; aday tekrar normal MİKRO değerlendirmesine döner.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path


REVIEW_FILE = Path(__file__).with_name("micro_site_field_review.json")
AUDIT_FILE = Path(__file__).with_name("micro_site_audit.json")
PENDING_FILE = Path(__file__).with_name("micro_pending_field_checks.json")
MATCH_RADIUS_M = 35
PENDING_STATUS = "SAHA_KONTROLU_BEKLIYOR"


def _number(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _distance_m(first, second):
    lat1 = _number(first.get("enlem"))
    lon1 = _number(first.get("boylam"))
    lat2 = _number(second.get("enlem"))
    lon2 = _number(second.get("boylam"))
    if None in (lat1, lon1, lat2, lon2):
        return float("inf")
    mean_lat = math.radians((lat1 + lat2) / 2)
    north_m = (lat2 - lat1) * 110570
    east_m = (lon2 - lon1) * 111320 * math.cos(mean_lat)
    return float(math.hypot(north_m, east_m))


def _parse_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text[:10], text):
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                pass
    return None


def _scene_dates(audit_payload):
    result = {}
    for region_key, region in (audit_payload.get("bolgeler") or {}).items():
        if isinstance(region, dict):
            result[str(region_key)] = str(region.get("son_tarih") or "")
    return result


def _eligible_pending_rows(payload):
    rows = []
    if not isinstance(payload, dict):
        return rows
    if payload.get("alarm") is not False or payload.get("saha_gorevi") is not False:
        return rows
    for raw in payload.get("kayitlar") or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("durum") or "").strip().upper() != PENDING_STATUS:
            continue
        if _number(raw.get("enlem")) is None or _number(raw.get("boylam")) is None:
            continue
        if _parse_date(raw.get("kaynak_sentinel_tarihi")) is None:
            continue
        rows.append(dict(raw))
    return rows


def _match(candidate, scene_date, pending_rows):
    current_scene = _parse_date(scene_date)
    if current_scene is None:
        return None
    matches = []
    for pending in pending_rows:
        pending_scene = _parse_date(pending.get("kaynak_sentinel_tarihi"))
        if pending_scene is None or pending_scene < current_scene:
            # Daha yeni Sentinel sahnesi eski manuel-dispatch kaydını bastırmasın.
            continue
        if str(pending.get("bolge") or "") not in ("", str(candidate.get("bolge") or "")):
            continue
        distance = _distance_m(candidate, pending)
        if distance <= MATCH_RADIUS_M:
            item = dict(pending)
            item["mesafe_m"] = round(distance, 1)
            matches.append(item)
    matches.sort(key=lambda item: (item["mesafe_m"], str(item.get("talep_tarihi") or "")))
    return matches[0] if matches else None


def apply_pending(review_payload, audit_payload, pending_payload):
    if not isinstance(review_payload, dict):
        raise ValueError("MİKRO saha review verisi geçersiz.")
    result = dict(review_payload)
    scene_dates = _scene_dates(audit_payload if isinstance(audit_payload, dict) else {})
    pending_rows = _eligible_pending_rows(pending_payload)

    active = []
    waiting = []
    for raw in review_payload.get("inceleme_adaylari") or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        scene_date = scene_dates.get(str(row.get("bolge") or ""), "")
        match = _match(row, scene_date, pending_rows)
        if match is None:
            row["saha_kontrolu_bekliyor"] = False
            active.append(row)
            continue
        row["saha_kontrolu_bekliyor"] = True
        row["bekleyen_kontrol_mesafe_m"] = match["mesafe_m"]
        row["bekleyen_kontrol_talep_tarihi"] = match.get("talep_tarihi")
        row["bekleyen_kontrol_kaynak_sentinel_tarihi"] = match.get("kaynak_sentinel_tarihi")
        row["bekleyen_kontrol_notu"] = match.get("not")
        waiting.append(row)

    result["inceleme_adaylari"] = active
    result["yeni_mikro_inceleme_adayi"] = len(active)
    result["saha_kontrolu_bekleyen_aday"] = len(waiting)
    result["saha_kontrolu_bekleyenler"] = waiting
    result["manuel_kontrol_esleme_yaricapi_m"] = MATCH_RADIUS_M
    result["not"] = (
        str(result.get("not") or "").rstrip()
        + " Manuel olarak sahaya gönderilmiş MİKRO adaylar aynı Sentinel sahnesinde tekrar yeni fırsat gibi gösterilmez; sonuç gelene kadar bekleyen-kontrol durumunda tutulur. Daha yeni Sentinel sahnesi eski bekleyen kaydı veto olarak kullanmaz."
    ).strip()
    result["alarm"] = False
    result["saha_gorevi"] = False
    result["ana_uretim_esigi_m2"] = 250
    result["mikro_aralik_m2"] = [150, 249]
    return result


def _self_check():
    review = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "yeni_mikro_inceleme_adayi": 1,
        "inceleme_adaylari": [
            {"bolge": "cesme", "enlem": 38.287679, "boylam": 26.266305, "alan_m2": 200}
        ],
        "not": "test",
    }
    pending = {
        "alarm": False,
        "saha_gorevi": False,
        "kayitlar": [
            {
                "durum": PENDING_STATUS,
                "bolge": "cesme",
                "enlem": 38.287679,
                "boylam": 26.266420,
                "kaynak_sentinel_tarihi": "08.09.2026",
                "talep_tarihi": "2026-09-11",
            }
        ],
    }
    same_scene = apply_pending(review, {"bolgeler": {"cesme": {"son_tarih": "08.09.2026"}}}, pending)
    assert same_scene["yeni_mikro_inceleme_adayi"] == 0
    assert same_scene["saha_kontrolu_bekleyen_aday"] == 1
    assert same_scene["alarm"] is False and same_scene["saha_gorevi"] is False
    assert same_scene["ana_uretim_esigi_m2"] == 250
    assert same_scene["mikro_aralik_m2"] == [150, 249]

    newer_scene = apply_pending(review, {"bolgeler": {"cesme": {"son_tarih": "11.09.2026"}}}, pending)
    assert newer_scene["yeni_mikro_inceleme_adayi"] == 1
    assert newer_scene["saha_kontrolu_bekleyen_aday"] == 0


def main():
    _self_check()
    if not REVIEW_FILE.exists() or not AUDIT_FILE.exists():
        raise RuntimeError("MİKRO saha review veya audit verisi bulunamadı.")
    review = json.loads(REVIEW_FILE.read_text(encoding="utf-8"))
    audit = json.loads(AUDIT_FILE.read_text(encoding="utf-8"))
    pending = {"alarm": False, "saha_gorevi": False, "kayitlar": []}
    if PENDING_FILE.exists():
        pending = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    result = apply_pending(review, audit, pending)
    REVIEW_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "MİKRO manuel saha bekleme köprüsü tamamlandı: "
        f"yeni={result['yeni_mikro_inceleme_adayi']}, "
        f"bekleyen={result['saha_kontrolu_bekleyen_aday']}. Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
