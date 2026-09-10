"""Gülbahçe taze güncel-sahne körlüğü için ara-gün devriye koruması.

Alarm veya kalıcı saha görevi üretmez. Gülbahçe'nin en yeni Sentinel sahnesinde
tarihsel kara olduğu doğrulanmış 250-6500 m² bir alan kalite/bulut nedeniyle kör
kalmışsa, normal iki günlük Gülbahçe duty devriyesi arasındaki tek günlük boşluğu
yalnız sahne 1-2 günlükken kapatır. Ana 250 m² üretim eşiği ve 150-249 m² MİKRO
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
            "guncel_sahne_yas_gun": source_age,
            "guncel_sahne_acil_kor_uygun_aday_sayisi": len(eligible),
        }
    )

    urgent = (
        not duty
        and east_key is not None
        and bool(eligible)
        and not already_covered
        and source_age is not None
        and MIN_URGENT_AGE_DAYS <= source_age <= MAX_URGENT_AGE_DAYS
    )
    if not urgent:
        report["kor_alan_saha_devriyesi"] = existing
        report["gulbahce_kor_alan_devriye_korumasi"] = guard
        return report

    # Ara-gün istisnası yalnız en küçük güvenli güncel kör hücreyi temsil eder.
    # Bir sonraki normal duty günü mevcut sahne-tarihli rotasyona devam eder.
    chosen = dict(eligible[0])
    report["kor_alan_saha_devriyesi"] = (other + [chosen])[:TOTAL_LIMIT]

    guard.update(
        {
            "uygulandi": True,
            "neden": "GULBAHCE_GUNCEL_SAHNE_KOR_ACIL_TEMSIL",
            "guncel_sahne_korlugu_kullanildi": True,
            "guncel_sahne_acil_kor_istisnasi": True,
            "secilen": {
                "enlem": chosen["enlem"],
                "boylam": chosen["boylam"],
                "alan_m2": chosen["alan_m2"],
                "neden": chosen["neden"],
                "referans_mesafesi_m": chosen["gulbahce_referans_mesafesi_m"],
                "cekirdek_operasyon": True,
                "guncel_sahne_korlugu": True,
                "kaynak_tarihi": chosen["gulbahce_guncel_sahne_tarihi"],
                "ara_gun_acil_koruma": True,
            },
        }
    )
    report["gulbahce_kor_alan_devriye_korumasi"] = guard
    report["kor_alan_saha_devriyesi_notu"] = (
        "Alarm/görev değildir. En yeni Gülbahçe Sentinel sahnesinde tarihsel kara "
        "kanıtlı 250-6500 m² gerçek kalite körlüğü 1-2 günlükken ve mevcut devriye "
        "onu kapsamıyorken, normal iki günlük duty döngüsünün ara gününde yalnız en "
        "küçük güvenli kör hücre doğu kapsama slotunda temsil edilir. Aktif görev ve "
        "bölgeler arası mesafe korumaları aynen uygulanır; 150-249 m² MİKRO katmanı "
        "bu istisnaya girmez."
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

    urgent_day = date(2026, 9, 10)
    assert _is_duty_day(urgent_day) is False
    assert _source_age_days(urgent_day, "08.09.2026") == 2
    guarded = apply_fresh_blind_urgency(
        current, audit, report, scan, rotation_day=urgent_day
    )
    meta = guarded["gulbahce_kor_alan_devriye_korumasi"]
    assert meta["guncel_sahne_acil_kor_istisnasi"] is True
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
    assert stale_day["gulbahce_kor_alan_devriye_korumasi"][
        "guncel_sahne_acil_kor_istisnasi"
    ] is False

    covered = dict(current)
    covered["secilen_devriye_guncel_korlugu_kapsiyor"] = True
    covered_result = apply_fresh_blind_urgency(
        covered, audit, report, scan, rotation_day=urgent_day
    )
    assert covered_result["gulbahce_kor_alan_devriye_korumasi"][
        "guncel_sahne_acil_kor_istisnasi"
    ] is False

    print("Gülbahçe taze güncel-körlük ara-gün koruması öz testi başarılı.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if args.check_only:
        return
    changed = update_fresh_blind_urgency()
    print(
        "Gülbahçe taze güncel-körlük ara-gün koruması güncellendi."
        if changed
        else "Gülbahçe taze güncel-körlük ara-gün korumasında değişiklik yok."
    )


if __name__ == "__main__":
    main()
