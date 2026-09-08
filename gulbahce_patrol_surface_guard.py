"""Gülbahçe kör-alan devriyesini güncel su/kıyı yüzeylerinden uzak tutar.

Bu katman şantiye alarmı veya saha görevi üretmez. Gülbahçe'nin en yeni
Sentinel SCL diagnostiginde tarihsel su, çözülememiş yüzey veya tarihsel kara
olup güncel SCL=su görünen bir kümeyle çakışan kör-alan devriye noktasını
operasyon rotasına sokmaz. Güvenli alternatif varsa aynı körlük havuzundan
başka bir Gülbahçe noktası seçilir; yoksa o slot boş bırakılır.

Amaç su/kıyı değişimini tamamen silmek değil, arka plandaki diagnostikte
korurken erken hafriyat/kazı için ayrılan insan devriyesini kara yüzeyine
odaklamaktır.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from coverage_patrol_shortlist import (
    FIELD_REPORT_MD,
    REPORT_JSON,
    TOTAL_LIMIT,
    _inject,
    _markdown,
    _point,
    _rotation_day,
)
from gulbahce_blind_patrol_guard import (
    AUDIT_JSON,
    DUTY_PERIOD_DAYS,
    GULBAHCE_CORE_RADIUS_M,
    GULBAHCE_OPERATION_RADIUS_M,
    GULBAHCE_SCAN_JSON,
    _current_non_east_patrol,
    _east_region,
    _eligible_gulbahce_candidates,
    _is_duty_day,
    _reference_point,
)

LATEST_STATE_JSON = Path(__file__).with_name("gulbahce_latest_state_blind_review.json")
MATCH_RADIUS_M = 25
BACKGROUND_KEYS = (
    "tarihsel_su_ornekleri",
    "cozulmemis_yuzey_ornekleri",
    "tarihsel_kara_guncel_su_ornekleri",
)


def _distance_m(first, second):
    # coverage_patrol_shortlist ile aynı yerel metre yaklaşımını tek noktadan kullan.
    from coverage_patrol_shortlist import _distance_m as distance_m

    return distance_m(first, second)


def _background_points(review_payload):
    points = []
    for key in BACKGROUND_KEYS:
        for item in (review_payload or {}).get(key) or []:
            point = _point(item)
            if point is not None:
                points.append(point)
    return points


def _matches_background(item, background_points):
    point = _point(item)
    if point is None:
        return False
    return any(
        _distance_m(point, background) <= MATCH_RADIUS_M
        for background in background_points
    )


def _review_matches_audit(review_payload, audit_payload):
    if (review_payload or {}).get("durum") != "ok":
        return False
    if (review_payload or {}).get("ayni_sentinel_sahnesi") is not True:
        return False
    _, east_data = _east_region(audit_payload)
    if not isinstance(east_data, dict):
        return False
    review_item = str((review_payload or {}).get("kaynak_son_item") or "")
    audit_item = str(east_data.get("son_item") or "")
    return bool(review_item and audit_item and review_item == audit_item)


def _is_gulbahce_patrol_item(item, east_key, scan_payload):
    """Duty bayrağı olmasa da Gülbahçe operasyon çevresine düşen doğu slotunu tanır."""
    if east_key is None or str(item.get("bolge_anahtari") or "") != str(east_key):
        return False
    point = _point(item)
    reference = _reference_point(scan_payload)
    if point is None or reference is None:
        return item.get("gulbahce_operasyonel_kapsama") is True
    return _distance_m(point, reference) <= GULBAHCE_OPERATION_RADIUS_M


def apply_surface_guard(
    audit_payload,
    report_payload,
    scan_payload,
    review_payload,
    rotation_day=None,
):
    report = dict(report_payload or {})
    day = rotation_day or _rotation_day(report)
    east_key, _ = _east_region(audit_payload)
    backgrounds = _background_points(review_payload)

    metadata = {
        "alarm": False,
        "saha_gorevi": False,
        "rotasyon_tarihi": day.isoformat(),
        "eslesme_yaricapi_m": MATCH_RADIUS_M,
        "inceleme_guncel": _review_matches_audit(review_payload, audit_payload),
        "arka_plan_kume_sayisi": len(backgrounds),
        "uygulandi": False,
        "elendi": 0,
        "neden": "DEGISIKLIK_YOK",
    }

    if not metadata["inceleme_guncel"]:
        metadata["neden"] = "GUNCEL_SAHNE_INCELEMESI_ESLESMIYOR"
        report["gulbahce_devriye_yuzey_korumasi"] = metadata
        return report

    current = [
        dict(item)
        for item in report.get("kor_alan_saha_devriyesi") or []
        if isinstance(item, dict)
    ]
    bad = [
        item for item in current
        if _is_gulbahce_patrol_item(item, east_key, scan_payload)
        and _matches_background(item, backgrounds)
    ]
    metadata["elendi"] = len(bad)
    if not bad:
        report["gulbahce_devriye_yuzey_korumasi"] = metadata
        return report

    # Sorunlu Gülbahçe slotunu önce çıkar; güvenli alternatif bulamazsak su/kıyı
    # noktasını operasyon listesinde bırakmamak, yanlış yönlendirmeden daha güvenlidir.
    kept = [item for item in current if item not in bad]
    report["kor_alan_saha_devriyesi"] = kept[:TOTAL_LIMIT]

    if east_key is None:
        metadata["uygulandi"] = True
        metadata["neden"] = "SU_KIYI_NOKTASI_CIKARILDI"
        report["gulbahce_devriye_yuzey_korumasi"] = metadata
        return report

    other = _current_non_east_patrol(report, east_key)
    _, eligible = _eligible_gulbahce_candidates(
        audit_payload,
        report,
        scan_payload,
        other,
    )
    safe = [item for item in eligible if not _matches_background(item, backgrounds)]
    core_safe = [
        item for item in safe if item.get("gulbahce_cekirdek_operasyon") is True
    ]
    pool = core_safe or safe

    if not pool:
        metadata["uygulandi"] = True
        metadata["neden"] = "GUVENLI_GULBAHCE_KOR_ALTERNATIFI_YOK"
        report["gulbahce_devriye_yuzey_korumasi"] = metadata
        return report

    if _is_duty_day(day):
        offset = (day.toordinal() // DUTY_PERIOD_DAYS) % len(pool)
    else:
        # Genel doğu rotasyonu Gülbahçe'ye doğal olarak düştüyse aynı günlük
        # çeşitliliği koru; yalnız su/kıyı ile çakışan hücreyi başka kara hücresine değiştir.
        offset = day.toordinal() % len(pool)
    chosen = dict(pool[offset])
    chosen["alarm"] = False
    chosen["saha_gorevi"] = False
    report["kor_alan_saha_devriyesi"] = (other + [chosen])[:TOTAL_LIMIT]
    metadata["uygulandi"] = True
    metadata["neden"] = "SU_KIYI_YERINE_GUVENLI_KARA_KOR_NOKTASI"
    metadata["secilen"] = {
        "enlem": chosen.get("enlem"),
        "boylam": chosen.get("boylam"),
        "alan_m2": chosen.get("alan_m2"),
        "cekirdek_operasyon": bool(chosen.get("gulbahce_cekirdek_operasyon")),
    }
    report["gulbahce_devriye_yuzey_korumasi"] = metadata
    return report


def _write_if_changed(path, text):
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def update_surface_guard():
    required = (AUDIT_JSON, REPORT_JSON, GULBAHCE_SCAN_JSON, LATEST_STATE_JSON)
    if not all(path.exists() for path in required):
        return False

    audit = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
    report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    scan = json.loads(GULBAHCE_SCAN_JSON.read_text(encoding="utf-8"))
    review = json.loads(LATEST_STATE_JSON.read_text(encoding="utf-8"))
    guarded = apply_surface_guard(audit, report, scan, review)

    changed = _write_if_changed(
        REPORT_JSON,
        json.dumps(guarded, ensure_ascii=False, indent=2) + "\n",
    )
    if FIELD_REPORT_MD.exists():
        current = FIELD_REPORT_MD.read_text(encoding="utf-8")
        changed = _write_if_changed(
            FIELD_REPORT_MD,
            _inject(current, _markdown(guarded.get("kor_alan_saha_devriyesi") or [])),
        ) or changed
    return changed


def _self_check():
    audit = {
        "bolgeler": {
            "east": {
                "durum": "ok",
                "bolge": "Uzunkuyu · Germiyan · Ildır · Gülbahçe",
                "son_item": "S2_TEST_NEW",
                "kara_referans_sahne_sayisi": 8,
                "kalan_kor_yuzde": 0.1,
                "kor_alan_devriye_ornekleri": [
                    {
                        "mahalle_yaklasik": "Gülbahçe",
                        "enlem": 38.330461,
                        "boylam": 26.652466,
                        "alan_m2": 400,
                        "neden": "KARISIK_GECERSIZLIK",
                    },
                    {
                        "mahalle_yaklasik": "Gülbahçe",
                        "enlem": 38.333000,
                        "boylam": 26.646000,
                        "alan_m2": 500,
                        "neden": "BULUT_GOLGE_KALICI",
                    },
                ],
            }
        }
    }
    scan = {
        "referanslar": {
            "coverage_guard": {"enlem": 38.33278, "boylam": 26.64556}
        }
    }
    report = {
        "rapor_tarihi": "2026-09-08",
        "saha_adaylari": [],
        "kor_alan_saha_devriyesi": [
            {
                "bolge_anahtari": "east",
                "bolge": "Uzunkuyu · Germiyan · Ildır · Gülbahçe",
                "mahalle": "Gülbahçe",
                "enlem": 38.330461,
                "boylam": 26.652466,
                "alan_m2": 400,
                "neden": "KARISIK_GECERSIZLIK",
                "alarm": False,
                "saha_gorevi": False,
            }
        ],
    }
    review = {
        "durum": "ok",
        "ayni_sentinel_sahnesi": True,
        "kaynak_son_item": "S2_TEST_NEW",
        "tarihsel_su_ornekleri": [],
        "cozulmemis_yuzey_ornekleri": [],
        "tarihsel_kara_guncel_su_ornekleri": [
            {"enlem": 38.330461, "boylam": 26.652466, "alan_m2": 400}
        ],
    }

    # 8 Eylül duty günü değildir: genel doğu rotasyonu Gülbahçe'yi seçmiş olsa
    # bile güncel SCL=su hücresi elenmeli ve güvenli kara alternatifiyle değişmelidir.
    guarded = apply_surface_guard(
        audit,
        report,
        scan,
        review,
        rotation_day=__import__("datetime").date(2026, 9, 8),
    )
    meta = guarded["gulbahce_devriye_yuzey_korumasi"]
    assert meta["inceleme_guncel"] is True
    assert meta["elendi"] == 1
    assert meta["uygulandi"] is True
    assert meta["neden"] == "SU_KIYI_YERINE_GUVENLI_KARA_KOR_NOKTASI"
    selected = guarded["kor_alan_saha_devriyesi"][-1]
    assert (selected["enlem"], selected["boylam"]) == (38.333, 26.646)
    assert selected["alarm"] is False and selected["saha_gorevi"] is False

    stale = dict(review)
    stale["kaynak_son_item"] = "S2_OLD"
    untouched = apply_surface_guard(
        audit,
        report,
        scan,
        stale,
        rotation_day=__import__("datetime").date(2026, 9, 8),
    )
    assert untouched["kor_alan_saha_devriyesi"][0]["enlem"] == 38.330461
    assert untouched["gulbahce_devriye_yuzey_korumasi"]["inceleme_guncel"] is False

    print("Gülbahçe kör-alan su/kıyı yüzey koruması öz testi başarılı.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if not args.check_only:
        changed = update_surface_guard()
        print(
            "Gülbahçe kör-alan su/kıyı yüzey koruması güncellendi."
            if changed
            else "Gülbahçe kör-alan su/kıyı yüzey korumasında değişiklik yok."
        )


if __name__ == "__main__":
    main()
