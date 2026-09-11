"""Manuel olarak sahaya gönderilen MİKRO adayları tekrar-dispatch'ten korur.

Bu katman 150-249 m² MİKRO politikasını değiştirmez, alarm veya kalıcı saha görevi
üretmez. Kullanıcının zaten saha kontrolü için personel gönderdiği bir MİKRO adayını
aynı Sentinel sahnesinde yeniden "yeni inceleme adayı" gibi göstermemek için yalnız
bekleyen-kontrol durumuna taşır. Manuel saha sonucu kaydedilmişse aynı Sentinel
sahnesindeki aday doğrulanmış kalibrasyon olarak arka plana alınır. Daha yeni Sentinel
sahnesi eski bekleyen veya doğrulanmış kaydı otomatik veto olarak kullanmaz.
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
RESOLVED_STATUS = "SAHA_KONTROLU_DOGRULANDI"
RESOLVED_OUTCOMES = {
    "SANTIYE_KAZI",
    "YIKIM_TEMIZLIK",
    "YOL_ALTYAPI",
    "TARLA_BITKI",
    "YANLIS_POZITIF",
}


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


def _eligible_rows(payload, expected_status):
    rows = []
    if not isinstance(payload, dict):
        return rows
    if payload.get("alarm") is not False or payload.get("saha_gorevi") is not False:
        return rows
    for raw in payload.get("kayitlar") or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("durum") or "").strip().upper() != expected_status:
            continue
        if _number(raw.get("enlem")) is None or _number(raw.get("boylam")) is None:
            continue
        if _parse_date(raw.get("kaynak_sentinel_tarihi")) is None:
            continue
        rows.append(dict(raw))
    return rows


def _eligible_pending_rows(payload):
    return _eligible_rows(payload, PENDING_STATUS)


def _eligible_resolved_rows(payload):
    rows = []
    for raw in _eligible_rows(payload, RESOLVED_STATUS):
        outcome = str(raw.get("sonuc") or "").strip().upper()
        if outcome not in RESOLVED_OUTCOMES:
            continue
        item = dict(raw)
        item["sonuc"] = outcome
        rows.append(item)
    return rows


def _match(candidate, scene_date, manual_rows):
    current_scene = _parse_date(scene_date)
    if current_scene is None:
        return None
    matches = []
    for manual in manual_rows:
        manual_scene = _parse_date(manual.get("kaynak_sentinel_tarihi"))
        if manual_scene is None or manual_scene < current_scene:
            # Daha yeni Sentinel sahnesi eski manuel kaydı bastırmasın.
            continue
        if str(manual.get("bolge") or "") not in ("", str(candidate.get("bolge") or "")):
            continue
        distance = _distance_m(candidate, manual)
        if distance <= MATCH_RADIUS_M:
            item = dict(manual)
            item["mesafe_m"] = round(distance, 1)
            matches.append(item)
    matches.sort(
        key=lambda item: (
            item["mesafe_m"],
            str(item.get("sonuc_tarihi") or item.get("talep_tarihi") or ""),
        )
    )
    return matches[0] if matches else None


def _resolved_background_row(row, match):
    updated = dict(row)
    updated["saha_kontrolu_bekliyor"] = False
    updated["guncel_saha_eslesmesi"] = True
    updated["saha_eslesme_mesafe_m"] = match["mesafe_m"]
    updated["saha_eslesme_gorev_id"] = match.get("kayit_id") or "MANUEL_MIKRO"
    updated["saha_eslesme_sonucu"] = match.get("sonuc")
    updated["manuel_saha_dogrulamasi"] = True
    updated["manuel_saha_dogrulama_tarihi"] = match.get("sonuc_tarihi")
    updated["manuel_saha_kanit_tipi"] = match.get("kanit_tipi")
    updated["manuel_saha_konum_dogrulama"] = match.get("konum_dogrulama")
    updated["saha_arka_plan_nedeni"] = (
        "Aynı Sentinel sahnesindeki MİKRO aday için manuel saha sonucu kaydedildi; "
        "yeni fırsat gibi tekrar gösterilmedi. Daha yeni Sentinel sahnesinde hareket "
        "yeniden oluşursa aday tekrar normal değerlendirmeye döner."
    )
    return updated


def apply_pending(review_payload, audit_payload, pending_payload):
    if not isinstance(review_payload, dict):
        raise ValueError("MİKRO saha review verisi geçersiz.")
    result = dict(review_payload)
    scene_dates = _scene_dates(audit_payload if isinstance(audit_payload, dict) else {})
    pending_rows = _eligible_pending_rows(pending_payload)
    resolved_rows = _eligible_resolved_rows(pending_payload)

    active = []
    waiting = []
    resolved_background = []
    for raw in review_payload.get("inceleme_adaylari") or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        scene_date = scene_dates.get(str(row.get("bolge") or ""), "")

        resolved_match = _match(row, scene_date, resolved_rows)
        if resolved_match is not None:
            resolved_background.append(_resolved_background_row(row, resolved_match))
            continue

        pending_match = _match(row, scene_date, pending_rows)
        if pending_match is None:
            row["saha_kontrolu_bekliyor"] = False
            active.append(row)
            continue
        row["saha_kontrolu_bekliyor"] = True
        row["bekleyen_kontrol_mesafe_m"] = pending_match["mesafe_m"]
        row["bekleyen_kontrol_talep_tarihi"] = pending_match.get("talep_tarihi")
        row["bekleyen_kontrol_kaynak_sentinel_tarihi"] = pending_match.get(
            "kaynak_sentinel_tarihi"
        )
        row["bekleyen_kontrol_notu"] = pending_match.get("not")
        waiting.append(row)

    background = [
        dict(row)
        for row in (review_payload.get("arka_plan_saha_eslesmeleri") or [])
        if isinstance(row, dict)
    ]
    background.extend(resolved_background)

    outcome_counts = dict(review_payload.get("guncel_saha_eslesmesi_sonuclari") or {})
    for row in resolved_background:
        outcome = str(row.get("saha_eslesme_sonucu") or "")
        if outcome:
            outcome_counts[outcome] = int(outcome_counts.get(outcome, 0) or 0) + 1

    result["inceleme_adaylari"] = active
    result["yeni_mikro_inceleme_adayi"] = len(active)
    result["arka_plan_saha_eslesmeleri"] = background
    result["guncel_saha_eslesmesi_arka_plan"] = len(background)
    result["guncel_saha_eslesmesi_sonuclari"] = outcome_counts
    result["saha_kontrolu_bekleyen_aday"] = len(waiting)
    result["saha_kontrolu_bekleyenler"] = waiting
    result["manuel_saha_sonucu_arka_plan"] = len(resolved_background)
    result["manuel_saha_sonuclari"] = resolved_background
    result["manuel_kontrol_esleme_yaricapi_m"] = MATCH_RADIUS_M
    result["not"] = (
        str(result.get("not") or "").rstrip()
        + " Manuel olarak sahaya gönderilmiş MİKRO adaylar aynı Sentinel sahnesinde "
        "tekrar yeni fırsat gibi gösterilmez; sonuç gelene kadar bekleyen-kontrol "
        "durumunda tutulur. Sonuç kaydedilirse aynı sahne kalibrasyon arka planına "
        "alınır. Daha yeni Sentinel sahnesi eski manuel kaydı veto olarak kullanmaz."
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
        "guncel_saha_eslesmesi_arka_plan": 0,
        "guncel_saha_eslesmesi_sonuclari": {
            "SANTIYE_KAZI": 0,
            "YIKIM_TEMIZLIK": 0,
            "YOL_ALTYAPI": 0,
            "TARLA_BITKI": 0,
            "YANLIS_POZITIF": 0,
        },
        "inceleme_adaylari": [
            {"bolge": "cesme", "enlem": 38.287679, "boylam": 26.266305, "alan_m2": 200}
        ],
        "arka_plan_saha_eslesmeleri": [],
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
    same_scene = apply_pending(
        review,
        {"bolgeler": {"cesme": {"son_tarih": "08.09.2026"}}},
        pending,
    )
    assert same_scene["yeni_mikro_inceleme_adayi"] == 0
    assert same_scene["saha_kontrolu_bekleyen_aday"] == 1
    assert same_scene["manuel_saha_sonucu_arka_plan"] == 0
    assert same_scene["alarm"] is False and same_scene["saha_gorevi"] is False
    assert same_scene["ana_uretim_esigi_m2"] == 250
    assert same_scene["mikro_aralik_m2"] == [150, 249]

    resolved = {
        "alarm": False,
        "saha_gorevi": False,
        "kayitlar": [
            {
                "kayit_id": "MM-TEST",
                "durum": RESOLVED_STATUS,
                "sonuc": "SANTIYE_KAZI",
                "bolge": "cesme",
                "enlem": 38.287679,
                "boylam": 26.266305,
                "kaynak_sentinel_tarihi": "08.09.2026",
                "sonuc_tarihi": "2026-09-11",
                "kanit_tipi": "KULLANICI_SAHA_FOTOGRAFI",
            }
        ],
    }
    resolved_same_scene = apply_pending(
        review,
        {"bolgeler": {"cesme": {"son_tarih": "08.09.2026"}}},
        resolved,
    )
    assert resolved_same_scene["yeni_mikro_inceleme_adayi"] == 0
    assert resolved_same_scene["saha_kontrolu_bekleyen_aday"] == 0
    assert resolved_same_scene["manuel_saha_sonucu_arka_plan"] == 1
    assert resolved_same_scene["guncel_saha_eslesmesi_arka_plan"] == 1
    assert resolved_same_scene["guncel_saha_eslesmesi_sonuclari"]["SANTIYE_KAZI"] == 1

    newer_scene = apply_pending(
        review,
        {"bolgeler": {"cesme": {"son_tarih": "11.09.2026"}}},
        resolved,
    )
    assert newer_scene["yeni_mikro_inceleme_adayi"] == 1
    assert newer_scene["saha_kontrolu_bekleyen_aday"] == 0
    assert newer_scene["manuel_saha_sonucu_arka_plan"] == 0


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
    REVIEW_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "MİKRO manuel saha köprüsü tamamlandı: "
        f"yeni={result['yeni_mikro_inceleme_adayi']}, "
        f"bekleyen={result['saha_kontrolu_bekleyen_aday']}, "
        f"doğrulanmış={result['manuel_saha_sonucu_arka_plan']}. "
        "Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
