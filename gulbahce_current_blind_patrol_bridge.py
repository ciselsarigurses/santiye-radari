"""Gülbahçe güncel-sahne körlüğünü iki günlük insan devriyesine güvenli biçimde bağlar.

Bu katman alarm veya kalıcı saha görevi üretmez. `gulbahce_latest_state_blind_review.json`
içinde EN YENİ Sentinel sahnesinde gerçekten bulut/gölge/geçersizlik nedeniyle görünmeyen,
tarihsel kara kanıtlı 250-6500 m² kümeler varsa, mevcut Gülbahçe duty gününde eski/tarihsel
körlük adayına göre bunları öncelemeyi sağlar. Duty olmayan günlerde rota değiştirilmez.

Amaç yeni hafriyatın tam da son sahnede bulut altında kaldığı durumda kör-alan devriyesini
stale iki-sahne körlüğü yerine güncel körlüğe yöneltmektir. 250 m² ana üretim eşiği ve
150-249 m² MİKRO politikası değişmez.
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
    MIN_AREA_M2,
    REPORT_JSON,
    TARGET_MAX_AREA_M2,
    TOTAL_LIMIT,
    _active_points,
    _distance_m,
    _far_enough,
    _inject,
    _markdown,
    _point,
    _rotation_day,
)
from gulbahce_blind_patrol_guard import (
    AUDIT_JSON,
    DUTY_PERIOD_DAYS,
    GULBAHCE_CORE_RADIUS_M,
    GULBAHCE_SCAN_JSON,
    _current_non_east_patrol,
    _east_region,
    _is_duty_day,
    _reference_point,
)

CURRENT_BLIND_JSON = Path(__file__).with_name("gulbahce_latest_state_blind_review.json")


def _eligible_current_blind(
    current_payload,
    audit_payload,
    report_payload,
    scan_payload,
    existing_other_patrol,
):
    east_key, east_data = _east_region(audit_payload)
    reference = _reference_point(scan_payload)
    if east_key is None or not isinstance(east_data, dict) or reference is None:
        return east_key, []
    if (current_payload or {}).get("durum") != "ok":
        return east_key, []
    if (current_payload or {}).get("ayni_sentinel_sahnesi") is not True:
        return east_key, []

    active = _active_points(report_payload)
    other_points = [
        point
        for item in existing_other_patrol
        if (point := _point(item)) is not None
    ]
    source_date = str((current_payload or {}).get("kaynak_son_tarih") or "")
    source_item = str((current_payload or {}).get("kaynak_son_item") or "")
    region_label = str(east_data.get("bolge") or east_key)

    eligible = []
    for raw in (current_payload or {}).get("guncel_sahne_kor_kumeleri") or []:
        if not isinstance(raw, dict):
            continue
        point = _point(raw)
        area = int(raw.get("alan_m2") or 0)
        if point is None or area < MIN_AREA_M2 or area > TARGET_MAX_AREA_M2:
            continue
        if str(raw.get("yuzey_kaniti") or "") != "TARIHSEL_KARA":
            continue
        distance_to_ref = _distance_m(point, reference)
        if distance_to_ref > GULBAHCE_CORE_RADIUS_M:
            continue
        if not _far_enough(point, active, ACTIVE_DISTANCE_M):
            continue
        if not _far_enough(point, other_points, CROSS_REGION_DISTANCE_M):
            continue

        candidate = {
            "bolge_anahtari": str(east_key),
            "bolge": region_label,
            "mahalle": "Gülbahçe · güncel uydu kör alanı",
            "enlem": round(point[0], 6),
            "boylam": round(point[1], 6),
            "alan_m2": area,
            "neden": str(raw.get("neden") or "GUNCEL_SAHNE_KALITE_KORLUGU"),
            "referans_sahne_sayisi": int(
                (current_payload or {}).get("kara_referans_sahne_sayisi") or 0
            ),
            "kalan_kor_yuzde": 0.0,
            "kaynak_tipi": "GULBAHCE_GUNCEL_SAHNE_BILINEN_KARA_KORLUGU",
            "alarm": False,
            "saha_gorevi": False,
            "durum": "KAPSAMA_KONTROLU",
            "gulbahce_operasyonel_kapsama": True,
            "gulbahce_cekirdek_operasyon": True,
            "gulbahce_referans_mesafesi_m": int(round(distance_to_ref)),
            "gulbahce_guncel_sahne_korlugu": True,
            "gulbahce_guncel_sahne_tarihi": source_date,
            "gulbahce_guncel_sahne_item": source_item,
        }
        candidate["harita"] = (
            "https://www.google.com/maps/dir/?api=1&destination="
            f"{candidate['enlem']:.6f},{candidate['boylam']:.6f}"
        )
        eligible.append(candidate)

    # Küçük/kompakt kör alanlar erken hafriyat açısından önce gelir. Aynı körlük
    # birden fazla duty gününde sürerse offset tüm güvenli adaylar arasında döner.
    eligible.sort(
        key=lambda item: (
            int(item.get("alan_m2") or 0),
            0 if item.get("neden") == "BULUT" else 1,
            int(item.get("gulbahce_referans_mesafesi_m") or 0),
            float(item.get("enlem") or 0),
            float(item.get("boylam") or 0),
        )
    )
    return east_key, eligible


def apply_current_blind_bridge(
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

    guard = dict(report.get("gulbahce_kor_alan_devriye_korumasi") or {})
    guard.update(
        {
            "alarm": False,
            "saha_gorevi": False,
            "aktif_gun": duty,
            "rotasyon_tarihi": day.isoformat(),
            "guncel_sahne_kor_kaynak_tarihi": str(
                (current_payload or {}).get("kaynak_son_tarih") or ""
            ) or None,
            "guncel_sahne_kor_uygun_aday_sayisi": len(eligible),
            "guncel_sahne_korlugu_kullanildi": False,
        }
    )

    if not duty or east_key is None or not eligible:
        report["kor_alan_saha_devriyesi"] = existing
        report["gulbahce_kor_alan_devriye_korumasi"] = guard
        return report

    offset = (day.toordinal() // DUTY_PERIOD_DAYS) % len(eligible)
    chosen = dict(eligible[offset])
    report["kor_alan_saha_devriyesi"] = (other + [chosen])[:TOTAL_LIMIT]

    guard["uygulandi"] = True
    guard["neden"] = "GULBAHCE_GUNCEL_SAHNE_KOR_TEMSIL"
    guard["guncel_sahne_korlugu_kullanildi"] = True
    guard["secilen"] = {
        "enlem": chosen["enlem"],
        "boylam": chosen["boylam"],
        "alan_m2": chosen["alan_m2"],
        "neden": chosen["neden"],
        "referans_mesafesi_m": chosen["gulbahce_referans_mesafesi_m"],
        "cekirdek_operasyon": True,
        "guncel_sahne_korlugu": True,
        "kaynak_tarihi": chosen["gulbahce_guncel_sahne_tarihi"],
    }
    report["gulbahce_kor_alan_devriye_korumasi"] = guard
    report["kor_alan_saha_devriyesi_notu"] = (
        "Alarm/görev değildir. Gülbahçe duty gününde en yeni Sentinel sahnesinde "
        "tarihsel kara kanıtlı gerçek 250-6500 m² kalite körlüğü varsa doğu devriye "
        "slotu önce bu güncel körlüğe ayrılır; uygun güncel körlük yoksa mevcut tarihsel "
        "kör-alan rotasyonu korunur. Aktif radar görevlerinden >=150 m ve diğer bölge "
        "devriyesinden >=250 m ayrım korunur."
    )
    return report


def _write_if_changed(path, text):
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def update_current_blind_bridge():
    required = (CURRENT_BLIND_JSON, AUDIT_JSON, REPORT_JSON, GULBAHCE_SCAN_JSON)
    if any(not path.exists() for path in required):
        return False

    current = json.loads(CURRENT_BLIND_JSON.read_text(encoding="utf-8"))
    audit = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
    report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    scan = json.loads(GULBAHCE_SCAN_JSON.read_text(encoding="utf-8"))
    bridged = apply_current_blind_bridge(current, audit, report, scan)

    changed = _write_if_changed(
        REPORT_JSON,
        json.dumps(bridged, ensure_ascii=False, indent=2) + "\n",
    )
    if FIELD_REPORT_MD.exists():
        current_md = FIELD_REPORT_MD.read_text(encoding="utf-8")
        section = _markdown(bridged.get("kor_alan_saha_devriyesi") or [])
        changed = _write_if_changed(FIELD_REPORT_MD, _inject(current_md, section)) or changed
    return changed


def _self_check():
    assert MIN_AREA_M2 == 250
    assert TARGET_MAX_AREA_M2 == 6500

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
        "rapor_tarihi": "2026-09-09",
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

    duty_day = date(2026, 9, 9)
    assert _is_duty_day(duty_day) is True
    bridged = apply_current_blind_bridge(
        current, audit, report, scan, rotation_day=duty_day
    )
    meta = bridged["gulbahce_kor_alan_devriye_korumasi"]
    assert meta["guncel_sahne_kor_uygun_aday_sayisi"] == 2
    assert meta["guncel_sahne_korlugu_kullanildi"] is True
    assert meta["secilen"]["guncel_sahne_korlugu"] is True
    east = [
        item
        for item in bridged["kor_alan_saha_devriyesi"]
        if item.get("bolge_anahtari") == "east"
    ]
    assert len(east) == 1
    assert east[0]["kaynak_tipi"] == "GULBAHCE_GUNCEL_SAHNE_BILINEN_KARA_KORLUGU"
    assert east[0]["alarm"] is False and east[0]["saha_gorevi"] is False

    non_duty = apply_current_blind_bridge(
        current, audit, report, scan, rotation_day=date(2026, 9, 8)
    )
    non_duty_meta = non_duty["gulbahce_kor_alan_devriye_korumasi"]
    assert non_duty_meta["guncel_sahne_korlugu_kullanildi"] is False
    assert non_duty["kor_alan_saha_devriyesi"][1]["mahalle"] == "Ildır"

    stale = dict(current)
    stale["durum"] = "veri_tarihi_uyusmuyor"
    stale_bridge = apply_current_blind_bridge(
        stale, audit, report, scan, rotation_day=duty_day
    )
    assert stale_bridge["gulbahce_kor_alan_devriye_korumasi"][
        "guncel_sahne_kor_uygun_aday_sayisi"
    ] == 0

    blocked_report = dict(report)
    blocked_report["saha_adaylari"] = [
        {
            "saha_durumu": "KONTROLE_GIT",
            "enlem": 38.34231,
            "boylam": 26.642621,
        }
    ]
    blocked = apply_current_blind_bridge(
        current, audit, blocked_report, scan, rotation_day=duty_day
    )
    assert blocked["gulbahce_kor_alan_devriye_korumasi"][
        "guncel_sahne_kor_uygun_aday_sayisi"
    ] == 1

    print("Gülbahçe güncel-sahne körlük devriye köprüsü öz testi başarılı.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if args.check_only:
        return
    changed = update_current_blind_bridge()
    print(
        "Gülbahçe güncel-sahne körlük devriye köprüsü güncellendi."
        if changed
        else "Gülbahçe güncel-sahne körlük devriye köprüsünde değişiklik yok."
    )


if __name__ == "__main__":
    main()
