"""Gülbahçe güncel-sahne körlüğü için ara-gün devriye koruması.

Alarm veya kalıcı saha görevi üretmez. Gülbahçe'nin en yeni Sentinel sahnesinde
tarihsel kara olduğu doğrulanmış 250-6500 m² bir alan kalite/bulut nedeniyle kör
kalmışsa, normal iki günlük Gülbahçe duty devriyesi arasındaki boşluğu kapatır.
Sahne 1-2 günlükken taze körlük istisnası uygulanır; daha yeni kullanılabilir bir
Sentinel sahnesi gelmedikçe eskiyen güncel-sahne körlükleri "görünürlük borcu"
olarak diagnostik rotasyonda tutulur. Ana 250 m² üretim eşiği ve 150-249 m² MİKRO
diagnostik politikası değişmez.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from coverage_patrol_shortlist import (
    FIELD_REPORT_MD,
    REPORT_JSON,
    TOTAL_LIMIT,
    _inject,
    _markdown,
    _rotation_day,
)
from gulbahce_blind_patrol_guard import (
    AUDIT_JSON,
    GULBAHCE_SCAN_JSON,
    _current_non_east_patrol,
    _east_region,
    _is_duty_day,
)
from gulbahce_current_blind_patrol_bridge import (
    CURRENT_BLIND_JSON,
    _eligible_current_blind,
    _parse_source_date,
)

MIN_URGENT_AGE_DAYS = 1
MAX_URGENT_AGE_DAYS = 2


def _source_age_days(day, source_date):
    source_day = _parse_source_date(source_date)
    if source_day is None or source_day > day:
        return None
    return (day - source_day).days


def _visibility_debt_offset(source_age, candidate_count):
    """Taze pencere sonrası aynı sahnenin kör kümelerini günlük sırayla döndür."""
    if candidate_count <= 1 or source_age is None:
        return 0
    debt_day = max(source_age - MAX_URGENT_AGE_DAYS - 1, 0)
    return debt_day % candidate_count


def apply_fresh_blind_urgency(
    current_payload,
    audit_payload,
    report_payload,
    scan_payload,
    rotation_day=None,
):
    report = dict(report_payload or {})
    day = rotation_day or _rotation_day(report)
    duty = _is_duty_day(day)
    east_key, _ = _east_region(audit_payload)

    existing = []
    for item in report.get("kor_alan_saha_devriyesi") or []:
        if not isinstance(item, dict):
            continue
        clean = dict(item)
        clean["alarm"] = False
        clean["saha_gorevi"] = False
        existing.append(clean)
    existing = existing[:TOTAL_LIMIT]

    other = _current_non_east_patrol(report, east_key)
    east_key, eligible = _eligible_current_blind(
        current_payload,
        audit_payload,
        report,
        scan_payload,
        other,
    )

    source_date = str((current_payload or {}).get("kaynak_son_tarih") or "")
    source_age = _source_age_days(day, source_date)
    already_covered = (
        (current_payload or {}).get("secilen_devriye_guncel_korlugu_kapsiyor") is True
        or any(
            item.get("gulbahce_guncel_sahne_korlugu") is True
            and str(item.get("gulbahce_guncel_sahne_tarihi") or "") == source_date
            for item in existing
        )
    )

    guard = dict(report.get("gulbahce_kor_alan_devriye_korumasi") or {})
    guard.update(
        {
            "alarm": False,
            "saha_gorevi": False,
            "guncel_sahne_acil_kor_istisnasi": False,
            "guncel_sahne_kalici_gorunurluk_borcu": False,
            "guncel_sahne_yas_gun": source_age,
            "guncel_sahne_acil_kor_uygun_aday_sayisi": len(eligible),
            "guncel_sahne_kalici_kor_rotasyon_offset": None,
        }
    )

    common = (
        not duty
        and east_key is not None
        and bool(eligible)
        and not already_covered
        and source_age is not None
    )
    urgent = common and MIN_URGENT_AGE_DAYS <= source_age <= MAX_URGENT_AGE_DAYS
    visibility_debt = common and source_age > MAX_URGENT_AGE_DAYS
    if not urgent and not visibility_debt:
        report["kor_alan_saha_devriyesi"] = existing
        report["gulbahce_kor_alan_devriye_korumasi"] = guard
        return report

    # Taze ara-günde en küçük güvenli kör hücreyi temsil et. En yeni kullanılabilir
    # sahne yaşlandıysa kör kümeyi unutmak yerine günlük deterministik rotasyonda tut.
    offset = 0 if urgent else _visibility_debt_offset(source_age, len(eligible))
    chosen = dict(eligible[offset])
    report["kor_alan_saha_devriyesi"] = (other + [chosen])[:TOTAL_LIMIT]

    reason = (
        "GULBAHCE_GUNCEL_SAHNE_KOR_ACIL_TEMSIL"
        if urgent
        else "GULBAHCE_GUNCEL_SAHNE_KALICI_GORUNURLUK_BORCU"
    )
    guard.update(
        {
            "uygulandi": True,
            "neden": reason,
            "guncel_sahne_korlugu_kullanildi": True,
            "guncel_sahne_acil_kor_istisnasi": urgent,
            "guncel_sahne_kalici_gorunurluk_borcu": visibility_debt,
            "guncel_sahne_kalici_kor_rotasyon_offset": offset if visibility_debt else None,
            "secilen": {
                "enlem": chosen["enlem"],
                "boylam": chosen["boylam"],
                "alan_m2": chosen["alan_m2"],
                "neden": chosen["neden"],
                "referans_mesafesi_m": chosen["gulbahce_referans_mesafesi_m"],
                "cekirdek_operasyon": True,
                "guncel_sahne_korlugu": True,
                "kaynak_tarihi": chosen["gulbahce_guncel_sahne_tarihi"],
                "ara_gun_acil_koruma": urgent,
                "kalici_gorunurluk_borcu": visibility_debt,
            },
        }
    )
    report["gulbahce_kor_alan_devriye_korumasi"] = guard
    report["kor_alan_saha_devriyesi_notu"] = (
        "Alarm/görev değildir. En yeni Gülbahçe Sentinel sahnesinde tarihsel kara "
        "kanıtlı 250-6500 m² gerçek kalite körlüğü mevcut devriye tarafından "
        "kapsanmıyorsa, 1-2 günlük taze pencerede en küçük güvenli kör hücre ara-gün "
        "istisnası olarak temsil edilir. Daha yeni kullanılabilir Sentinel sahnesi "
        "gelmezse aynı en-yeni sahnenin çözülmemiş kara-kör kümeleri görünürlük borcu "
        "olarak günlük deterministik rotasyonda tutulur; böylece eski tarihsel devriye "
        "noktası güncel körlüğün önüne geçmez. Aktif görev ve bölgeler arası mesafe "
        "korumaları aynen uygulanır; 150-249 m² MİKRO katmanı bu istisnaya girmez."
    )
    return report


def _write_if_changed(path, text):
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def update_fresh_blind_urgency():
    required = (CURRENT_BLIND_JSON, AUDIT_JSON, REPORT_JSON, GULBAHCE_SCAN_JSON)
    if any(not path.exists() for path in required):
        return False

    current = json.loads(CURRENT_BLIND_JSON.read_text(encoding="utf-8"))
    audit = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
    report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    scan = json.loads(GULBAHCE_SCAN_JSON.read_text(encoding="utf-8"))
    guarded = apply_fresh_blind_urgency(current, audit, report, scan)

    changed = _write_if_changed(
        REPORT_JSON,
        json.dumps(guarded, ensure_ascii=False, indent=2) + "\n",
    )
    if FIELD_REPORT_MD.exists():
        current_md = FIELD_REPORT_MD.read_text(encoding="utf-8")
        section = _markdown(guarded.get("kor_alan_saha_devriyesi") or [])
        changed = _write_if_changed(
            FIELD_REPORT_MD,
            _inject(current_md, section),
        ) or changed
    return changed


def _fixtures():
    audit = {
        "bolgeler": {
            "east": {
                "durum": "ok",
                "bolge": "Uzunkuyu · Germiyan · Ildır · Gülbahçe",
                "kor_alan_devriye_ornekleri": [],
            }
        }
    }
    scan = {
        "referanslar": {
            "coverage_guard": {"enlem": 38.33278, "boylam": 26.64556}
        }
    }
    current = {
        "durum": "ok",
        "ayni_sentinel_sahnesi": True,
        "kaynak_son_tarih": "08.09.2026",
        "kaynak_son_item": "S2_CURRENT",
        "kara_referans_sahne_sayisi": 8,
        "secilen_devriye_guncel_korlugu_kapsiyor": False,
        "guncel_sahne_kor_kumeleri": [
            {
                "enlem": 38.34231,
                "boylam": 26.642621,
                "alan_m2": 400,
                "neden": "BULUT",
                "yuzey_kaniti": "TARIHSEL_KARA",
            },
            {
                "enlem": 38.346923,
                "boylam": 26.641132,
                "alan_m2": 1600,
                "neden": "BULUT",
                "yuzey_kaniti": "TARIHSEL_KARA",
            },
        ],
    }
    report = {
        "rapor_tarihi": "2026-09-10",
        "saha_adaylari": [],
        "kor_alan_saha_devriyesi": [
            {
                "bolge_anahtari": "west",
                "bolge": "Çeşme",
                "mahalle": "Alaçatı",
                "enlem": 38.2800,
                "boylam": 26.3700,
                "alan_m2": 400,
                "neden": "BULUT_GOLGE_KALICI",
                "alarm": False,
                "saha_gorevi": False,
            },
            {
                "bolge_anahtari": "east",
                "bolge": "Uzunkuyu · Germiyan · Ildır · Gülbahçe",
                "mahalle": "Ildır",
                "enlem": 38.4000,
                "boylam": 26.4700,
                "alan_m2": 800,
                "neden": "BULUT_GOLGE_KALICI",
                "alarm": False,
                "saha_gorevi": False,
            },
        ],
    }
    return current, audit, report, scan


def _self_check():
    current, audit, report, scan = _fixtures()

    assert _visibility_debt_offset(3, 2) == 0
    assert _visibility_debt_offset(4, 2) == 1
    assert _visibility_debt_offset(5, 2) == 0

    urgent_day = date(2026, 9, 10)
    assert _is_duty_day(urgent_day) is False
    assert _source_age_days(urgent_day, "08.09.2026") == 2
    guarded = apply_fresh_blind_urgency(
        current, audit, report, scan, rotation_day=urgent_day
    )
    meta = guarded["gulbahce_kor_alan_devriye_korumasi"]
    assert meta["guncel_sahne_acil_kor_istisnasi"] is True
    assert meta["guncel_sahne_kalici_gorunurluk_borcu"] is False
    assert meta["secilen"]["alan_m2"] == 400
    east = [
        item
        for item in guarded["kor_alan_saha_devriyesi"]
        if item.get("bolge_anahtari") == "east"
    ]
    assert len(east) == 1
    assert east[0]["alarm"] is False and east[0]["saha_gorevi"] is False

    same_day = apply_fresh_blind_urgency(
        current, audit, report, scan, rotation_day=date(2026, 9, 8)
    )
    assert same_day["gulbahce_kor_alan_devriye_korumasi"][
        "guncel_sahne_acil_kor_istisnasi"
    ] is False
    assert same_day["kor_alan_saha_devriyesi"][1]["mahalle"] == "Ildır"

    stale_day = apply_fresh_blind_urgency(
        current, audit, report, scan, rotation_day=date(2026, 9, 12)
    )
    stale_meta = stale_day["gulbahce_kor_alan_devriye_korumasi"]
    assert stale_meta["guncel_sahne_acil_kor_istisnasi"] is False
    assert stale_meta["guncel_sahne_kalici_gorunurluk_borcu"] is True
    assert stale_meta["guncel_sahne_kalici_kor_rotasyon_offset"] == 1
    assert stale_meta["secilen"]["alan_m2"] == 1600
    assert stale_meta["secilen"]["kalici_gorunurluk_borcu"] is True

    covered = dict(current)
    covered["secilen_devriye_guncel_korlugu_kapsiyor"] = True
    covered_result = apply_fresh_blind_urgency(
        covered, audit, report, scan, rotation_day=urgent_day
    )
    assert covered_result["gulbahce_kor_alan_devriye_korumasi"][
        "guncel_sahne_acil_kor_istisnasi"
    ] is False
    assert covered_result["gulbahce_kor_alan_devriye_korumasi"][
        "guncel_sahne_kalici_gorunurluk_borcu"
    ] is False

    print("Gülbahçe güncel-körlük ara-gün/görünürlük-borcu öz testi başarılı.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if args.check_only:
        return
    changed = update_fresh_blind_urgency()
    print(
        "Gülbahçe güncel-körlük ara-gün/görünürlük-borcu koruması güncellendi."
        if changed
        else "Gülbahçe güncel-körlük ara-gün/görünürlük-borcu korumasında değişiklik yok."
    )


if __name__ == "__main__":
    main()
