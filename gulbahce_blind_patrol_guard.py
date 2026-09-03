"""Gülbahçe kör-alan devriyesi için iki günlük güvenli temsil koruması.

Bu katman şantiye alarmı veya kalıcı saha görevi üretmez. Mevcut
coverage_patrol_shortlist çıktısındaki doğu bölgesi slotunu, yalnız uygun bir
bilinen-kara kör hücresi varsa iki günde bir Gülbahçe operasyonel referansı
çevresine ayırır. Diğer gün mevcut Uzunkuyu/Germiyan/Ildır/Gülbahçe rotasyonu
değişmeden kalır.

Gülbahçe referansı yalnız operasyonel kapsama noktasıdır; idari/kadastral sınır
olarak kullanılmaz.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from coverage_patrol_shortlist import (
    ACTIVE_DISTANCE_M,
    CROSS_REGION_DISTANCE_M,
    FIELD_REPORT_MD,
    REPORT_JSON,
    TARGET_MAX_AREA_M2,
    TOTAL_LIMIT,
    _active_points,
    _candidate_pool,
    _distance_m,
    _far_enough,
    _inject,
    _markdown,
    _point,
    _rotation_day,
)

AUDIT_JSON = Path(__file__).with_name("coverage_blind_area_audit.json")
GULBAHCE_SCAN_JSON = Path(__file__).with_name("gulbahce_east_strip_scan.json")

GULBAHCE_OPERATION_RADIUS_M = 3000
DUTY_PERIOD_DAYS = 2


def _reference_point(scan_payload):
    refs = (scan_payload or {}).get("referanslar") or {}
    guard = refs.get("coverage_guard") or {}
    try:
        return float(guard["enlem"]), float(guard["boylam"])
    except (KeyError, TypeError, ValueError):
        return None


def _east_region(audit_payload):
    regions = (audit_payload or {}).get("bolgeler") or {}
    for key, data in regions.items():
        label = str((data or {}).get("bolge") or "")
        if "gülbahçe" in label.casefold():
            return str(key), data
    return None, None


def _is_duty_day(day):
    return day.toordinal() % DUTY_PERIOD_DAYS == 0


def _current_non_east_patrol(report_payload, east_key):
    result = []
    for item in (report_payload or {}).get("kor_alan_saha_devriyesi") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("bolge_anahtari") or "") == str(east_key):
            continue
        clean = dict(item)
        clean["alarm"] = False
        clean["saha_gorevi"] = False
        result.append(clean)
    return result[: max(TOTAL_LIMIT - 1, 0)]


def _eligible_gulbahce_candidates(
    audit_payload,
    report_payload,
    scan_payload,
    existing_other_patrol,
):
    east_key, east_data = _east_region(audit_payload)
    reference = _reference_point(scan_payload)
    if east_key is None or not isinstance(east_data, dict) or reference is None:
        return east_key, []

    active = _active_points(report_payload)
    other_points = [
        point for item in existing_other_patrol
        if (point := _point(item)) is not None
    ]

    eligible = []
    for item in _candidate_pool(east_key, east_data):
        point = _point(item)
        if point is None:
            continue
        if int(item.get("alan_m2") or 0) > TARGET_MAX_AREA_M2:
            continue
        distance_to_ref = _distance_m(point, reference)
        if distance_to_ref > GULBAHCE_OPERATION_RADIUS_M:
            continue
        if not _far_enough(point, active, ACTIVE_DISTANCE_M):
            continue
        if not _far_enough(point, other_points, CROSS_REGION_DISTANCE_M):
            continue

        candidate = dict(item)
        candidate["alarm"] = False
        candidate["saha_gorevi"] = False
        candidate["gulbahce_operasyonel_kapsama"] = True
        candidate["gulbahce_referans_mesafesi_m"] = int(round(distance_to_ref))
        eligible.append(candidate)

    eligible.sort(
        key=lambda item: (
            0 if item.get("neden") == "BULUT_GOLGE_KALICI" else 1,
            int(item.get("alan_m2") or 0),
            int(item.get("gulbahce_referans_mesafesi_m") or 0),
            float(item.get("enlem") or 0),
            float(item.get("boylam") or 0),
        )
    )
    return east_key, eligible


def apply_gulbahce_guard(
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
        if isinstance(item, dict):
            clean = dict(item)
            clean["alarm"] = False
            clean["saha_gorevi"] = False
            existing.append(clean)
    existing = existing[:TOTAL_LIMIT]

    metadata = {
        "alarm": False,
        "saha_gorevi": False,
        "aktif_gun": duty,
        "rotasyon_tarihi": day.isoformat(),
        "operasyonel_yaricap_m": GULBAHCE_OPERATION_RADIUS_M,
        "idari_kadastral_sinir_degildir": True,
        "uygulandi": False,
        "uygun_aday_sayisi": 0,
        "neden": "GENEL_DOGU_ROTASYONU",
    }

    if not duty:
        report["kor_alan_saha_devriyesi"] = existing
        report["gulbahce_kor_alan_devriye_korumasi"] = metadata
        return report

    other = _current_non_east_patrol(report, east_key)
    east_key, eligible = _eligible_gulbahce_candidates(
        audit_payload,
        report,
        scan_payload,
        other,
    )
    metadata["uygun_aday_sayisi"] = len(eligible)

    if east_key is None:
        metadata["neden"] = "DOGU_BOLGESI_BULUNAMADI"
    elif not eligible:
        metadata["neden"] = "UYGUN_GULBAHCE_KOR_HUCRESI_YOK"
    else:
        # Duty günleri iki günde bir geldiği için // DUTY_PERIOD_DAYS kullanmak,
        # çift sayıda aday olduğunda listenin yalnız yarısında dönüp durmayı önler.
        offset = (day.toordinal() // DUTY_PERIOD_DAYS) % len(eligible)
        chosen = dict(eligible[offset])
        selected = other + [chosen]
        report["kor_alan_saha_devriyesi"] = selected[:TOTAL_LIMIT]
        metadata["uygulandi"] = True
        metadata["neden"] = "GULBAHCE_OPERASYONEL_TEMSIL"
        metadata["secilen"] = {
            "enlem": chosen["enlem"],
            "boylam": chosen["boylam"],
            "alan_m2": chosen["alan_m2"],
            "neden": chosen["neden"],
            "referans_mesafesi_m": chosen["gulbahce_referans_mesafesi_m"],
        }

    if not metadata["uygulandi"]:
        report["kor_alan_saha_devriyesi"] = existing

    report["gulbahce_kor_alan_devriye_korumasi"] = metadata
    report["kor_alan_saha_devriyesi_notu"] = (
        "Alarm/görev değildir. Mevcut bölgesel kör-alan rotasyonu korunur; iki günde "
        "bir uygun 250-6500 m² bilinen-kara kör hücresi varsa doğu slotu Gülbahçe "
        "operasyonel referansının 3 km çevresine ayrılır. Aktif radar görevlerinden "
        ">=150 m ve diğer kör-alan devriyesinden >=250 m uzakta tutulur. Operasyonel "
        "referans idari/kadastral sınır değildir."
    )
    return report


def _write_if_changed(path, text):
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def update_gulbahce_guard():
    if not AUDIT_JSON.exists() or not REPORT_JSON.exists() or not GULBAHCE_SCAN_JSON.exists():
        return False

    audit = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
    report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    scan = json.loads(GULBAHCE_SCAN_JSON.read_text(encoding="utf-8"))
    guarded = apply_gulbahce_guard(audit, report, scan)

    changed = _write_if_changed(
        REPORT_JSON,
        json.dumps(guarded, ensure_ascii=False, indent=2) + "\n",
    )

    if FIELD_REPORT_MD.exists():
        current = FIELD_REPORT_MD.read_text(encoding="utf-8")
        section = _markdown(guarded.get("kor_alan_saha_devriyesi") or [])
        changed = _write_if_changed(
            FIELD_REPORT_MD,
            _inject(current, section),
        ) or changed

    return changed


def _self_check():
    audit = {
        "bolgeler": {
            "west": {
                "durum": "ok",
                "bolge": "Çeşme",
                "kor_alan_devriye_ornekleri": [],
            },
            "east": {
                "durum": "ok",
                "bolge": "Uzunkuyu · Germiyan · Ildır · Gülbahçe",
                "kara_referans_sahne_sayisi": 8,
                "kalan_kor_yuzde": 0.1,
                "kor_alan_devriye_ornekleri": [
                    {
                        "mahalle_yaklasik": "Mevki doğrulanmadı",
                        "enlem": 38.3330,
                        "boylam": 26.6460,
                        "alan_m2": 400,
                        "neden": "BULUT_GOLGE_KALICI",
                    },
                    {
                        "mahalle_yaklasik": "Mevki doğrulanmadı",
                        "enlem": 38.3400,
                        "boylam": 26.6500,
                        "alan_m2": 500,
                        "neden": "KARISIK_GECERSIZLIK",
                    },
                ],
            },
        }
    }
    scan = {
        "referanslar": {
            "coverage_guard": {"enlem": 38.33278, "boylam": 26.64556}
        }
    }
    report = {
        "rapor_tarihi": "2026-09-03",
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
            },
        ],
    }

    duty_day = date(2026, 9, 3)
    guarded = apply_gulbahce_guard(audit, report, scan, rotation_day=duty_day)
    meta = guarded["gulbahce_kor_alan_devriye_korumasi"]
    assert meta["aktif_gun"] is True
    assert meta["uygulandi"] is True
    assert len(guarded["kor_alan_saha_devriyesi"]) <= TOTAL_LIMIT
    east = [
        item for item in guarded["kor_alan_saha_devriyesi"]
        if item.get("bolge_anahtari") == "east"
    ]
    assert len(east) == 1
    assert east[0]["gulbahce_operasyonel_kapsama"] is True
    assert east[0]["alarm"] is False and east[0]["saha_gorevi"] is False

    alternate = apply_gulbahce_guard(
        audit,
        report,
        scan,
        rotation_day=date(2026, 9, 4),
    )
    assert alternate["gulbahce_kor_alan_devriye_korumasi"]["aktif_gun"] is False
    assert alternate["kor_alan_saha_devriyesi"][1]["mahalle"] == "Ildır"

    blocked_report = dict(report)
    blocked_report["saha_adaylari"] = [
        {
            "saha_durumu": "KONTROLE_GIT",
            "enlem": 38.3330,
            "boylam": 26.6460,
        },
        {
            "saha_durumu": "KONTROLE_GIT",
            "enlem": 38.3400,
            "boylam": 26.6500,
        },
    ]
    blocked = apply_gulbahce_guard(
        audit,
        blocked_report,
        scan,
        rotation_day=duty_day,
    )
    assert blocked["gulbahce_kor_alan_devriye_korumasi"]["uygulandi"] is False

    print("Gülbahçe kör-alan devriye koruması öz testi başarılı.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if not args.check_only:
        changed = update_gulbahce_guard()
        print(
            "Gülbahçe kör-alan devriye koruması güncellendi."
            if changed
            else "Gülbahçe kör-alan devriye korumasında değişiklik yok."
        )


if __name__ == "__main__":
    main()
