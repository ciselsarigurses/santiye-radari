"""Gülbahçe güncel kör alan devriyesini güçlü SAR+temporal kanıtıyla güvenli biçimde önceler.

Bu katman yalnız mevcut, tarihsel-kara kanıtlı 250-6500 m² Gülbahçe güncel-sahne
körlük adayları arasında sıralama yapar. Yeni aday üretmez; alarm veya kalıcı saha görevi
oluşturmaz. Sentinel-1 haritasındaki güçlü çift-polarizasyonlu kompakt/lokal değişim ve
temporal başlangıç/devam kanıtı, optik sahneden daha yeniyse normal iki günlük rotasyona
geçici öncelik verebilir. 150-249 m² MİKRO katmanı bu köprüye girmez.

15 Eylül 2026 ve sonrasında birden fazla uygun SAR destekli kör alan varsa, sezon açılışı
sonrası yeni SAR kanıtı önce gelir; aynı bantta daha yeni sahne ve ardından daha güçlü skor
tercih edilir. Sezon öncesi güçlü kanıt silinmez ve post-sezon kanıt yoksa güvenli fallback
olarak izlenmeye devam eder.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from coverage_patrol_shortlist import (
    FIELD_REPORT_MD,
    MIN_AREA_M2,
    REPORT_JSON,
    TARGET_MAX_AREA_M2,
    TOTAL_LIMIT,
    _distance_m,
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

SAR_MAP_GEOJSON = Path(__file__).with_name("sentinel1_rtc_map.geojson")
SAR_MATCH_RADIUS_M = 35.0
MIN_STRONG_SCORE_DB = 2.0
FULL_OPERATION_START = date(2026, 9, 15)
SUPPORTED_TEMPORAL = {
    "ANI_YENI_LOKAL_BASLANGIC_DESTEKLI",
    "ARDISIK_GUCLU_LOKAL_HAREKET",
    "ARDISIK_ORTA_LOKAL_HAREKET",
}


def _iso_date(value):
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _operational_sar_sort_key(item, operational_day=None):
    """Sezon açıldıktan sonra taze post-sezon SAR'ı eski kalibrasyon kanıtının önüne al."""
    sar_day = _iso_date(item.get("gulbahce_sar_yeni_tarih"))
    score = float(item.get("gulbahce_sar_lokal_degisim_skor_db") or 0.0)
    area = int(item.get("alan_m2") or 0)
    lat = float(item.get("enlem") or 0.0)
    lon = float(item.get("boylam") or 0.0)

    if operational_day is not None and operational_day >= FULL_OPERATION_START:
        post_season = sar_day is not None and sar_day >= FULL_OPERATION_START
        # Post-sezon kanıt varsa önce onu, sonra en yeni sahneyi, sonra daha güçlü skoru seç.
        return (
            0 if post_season else 1,
            -(sar_day.toordinal() if post_season else 0),
            -score,
            area,
            lat,
            lon,
        )

    return (0, 0, -score, area, lat, lon)


def _sar_supported_blind_candidates(
    eligible,
    sar_payload,
    optical_source_date,
    operational_day=None,
):
    """Yalnız güçlü, kompakt ve optikten daha yeni SAR kanıtını eşleştir."""
    optical_day = _parse_source_date(optical_source_date)
    if optical_day is None:
        return []

    supported = []
    seen = set()
    for feature in (sar_payload or {}).get("features") or []:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        if (
            str(geometry.get("type") or "") != "Point"
            or not isinstance(coords, list)
            or len(coords) < 2
        ):
            continue

        lon = _num(coords[0])
        lat = _num(coords[1])
        score = _num(props.get("sar_lokal_degisim_skor_db"))
        area = int(_num(props.get("alan_m2")) or 0)
        sar_day = _iso_date(props.get("yeni_tarih"))
        if lat is None or lon is None or score is None or sar_day is None:
            continue
        if str(props.get("bolge") or "").strip().lower() != "gulbahce":
            continue
        if str(props.get("kaynak") or "") != "gulbahce_latest_state_blind_review.json":
            continue
        if str(props.get("harita_katmani") or "") != "SAR_GUCLU_LOKAL":
            continue
        if props.get("arka_plan") is not False:
            continue
        if props.get("alarm") is not False or props.get("saha_gorevi") is not False:
            continue
        if str(props.get("hedef_katmani") or "") != "ANA_250_PLUS":
            continue
        if area < MIN_AREA_M2 or area > TARGET_MAX_AREA_M2:
            continue
        if score < MIN_STRONG_SCORE_DB:
            continue
        if str(props.get("sar_polarizasyon_uyumu") or "") != "CIFT_POL_GUCLU":
            continue
        if str(props.get("sar_mekansal_ayrim") or "") != "KOMPAKT_LOKAL_DESTEKLI":
            continue
        temporal = str(props.get("temporal_durum") or "")
        if temporal not in SUPPORTED_TEMPORAL:
            continue
        # SAR kanıtı aynı/eski optik sahneden geliyorsa rotasyonu ezmesin.
        if sar_day <= optical_day:
            continue

        nearest = None
        nearest_distance = None
        for candidate in eligible:
            point = (
                _num(candidate.get("enlem")),
                _num(candidate.get("boylam")),
            )
            if point[0] is None or point[1] is None:
                continue
            distance = _distance_m((lat, lon), point)
            if distance > SAR_MATCH_RADIUS_M:
                continue
            if nearest_distance is None or distance < nearest_distance:
                nearest = candidate
                nearest_distance = distance

        if nearest is None:
            continue
        key = (
            round(float(nearest["enlem"]), 6),
            round(float(nearest["boylam"]), 6),
        )
        if key in seen:
            continue
        seen.add(key)

        item = dict(nearest)
        item.update(
            {
                "gulbahce_sar_destekli_kor_onceligi": True,
                "gulbahce_sar_lokal_degisim_skor_db": round(score, 3),
                "gulbahce_sar_temporal_durum": temporal,
                "gulbahce_sar_yeni_tarih": sar_day.isoformat(),
                "gulbahce_sar_esleme_mesafesi_m": round(float(nearest_distance), 1),
            }
        )
        supported.append(item)

    supported.sort(key=lambda item: _operational_sar_sort_key(item, operational_day))
    return supported


def apply_sar_blind_priority(
    current_payload,
    audit_payload,
    report_payload,
    scan_payload,
    sar_payload,
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
    optical_source_date = str((current_payload or {}).get("kaynak_son_tarih") or "")
    supported = _sar_supported_blind_candidates(
        eligible,
        sar_payload,
        optical_source_date,
        operational_day=day,
    )
    post_season_supported = [
        item
        for item in supported
        if (_iso_date(item.get("gulbahce_sar_yeni_tarih")) or date.min)
        >= FULL_OPERATION_START
    ]

    guard = dict(report.get("gulbahce_kor_alan_devriye_korumasi") or {})
    guard.update(
        {
            "alarm": False,
            "saha_gorevi": False,
            "sar_destekli_kor_onceligi_kullanildi": False,
            "sar_destekli_kor_uygun_aday_sayisi": len(supported),
            "sar_15eylul_taze_oncelik_aktif": day >= FULL_OPERATION_START,
            "sar_15eylul_taze_uygun_aday_sayisi": len(post_season_supported),
            "sar_destekli_kor_kural": (
                "GUNCEL_KOR_250_PLUS+SAR_GUCLU_LOKAL+CIFT_POL+KOMPAKT+"
                "TEMPORAL_DESTEK+SAR_OPTIKTEN_YENI"
            ),
        }
    )

    if not duty or east_key is None or not supported:
        report["kor_alan_saha_devriyesi"] = existing
        report["gulbahce_kor_alan_devriye_korumasi"] = guard
        return report

    chosen = dict(supported[0])
    chosen["alarm"] = False
    chosen["saha_gorevi"] = False
    chosen_sar_day = _iso_date(chosen.get("gulbahce_sar_yeni_tarih"))
    chosen["gulbahce_sar_15eylul_sonrasi"] = bool(
        chosen_sar_day is not None and chosen_sar_day >= FULL_OPERATION_START
    )
    report["kor_alan_saha_devriyesi"] = (other + [chosen])[:TOTAL_LIMIT]

    guard.update(
        {
            "uygulandi": True,
            "neden": "GULBAHCE_GUNCEL_KOR_SAR_TEMPORAL_ONCELIK",
            "guncel_sahne_korlugu_kullanildi": True,
            "sar_destekli_kor_onceligi_kullanildi": True,
            "secilen": {
                "enlem": chosen["enlem"],
                "boylam": chosen["boylam"],
                "alan_m2": chosen["alan_m2"],
                "neden": chosen["neden"],
                "guncel_sahne_korlugu": True,
                "kaynak_tarihi": chosen["gulbahce_guncel_sahne_tarihi"],
                "sar_lokal_degisim_skor_db": chosen[
                    "gulbahce_sar_lokal_degisim_skor_db"
                ],
                "sar_temporal_durum": chosen["gulbahce_sar_temporal_durum"],
                "sar_yeni_tarih": chosen["gulbahce_sar_yeni_tarih"],
                "sar_esleme_mesafesi_m": chosen["gulbahce_sar_esleme_mesafesi_m"],
                "sar_15eylul_sonrasi": chosen["gulbahce_sar_15eylul_sonrasi"],
            },
        }
    )
    report["gulbahce_kor_alan_devriye_korumasi"] = guard
    report["kor_alan_saha_devriyesi_notu"] = (
        "Alarm/görev değildir. Gülbahçe duty gününde yalnız en yeni optik sahnede "
        "tarihsel-kara kanıtlı 250-6500 m² güncel körlük adaylarından biri, optik "
        "sahneden daha yeni Sentinel-1 verisinde çift-polarizasyon güçlü, kompakt/lokal "
        "ve temporal başlangıç/devam kanıtı taşıyorsa normal iki günlük körlük "
        "rotasyonuna geçici öncelik verir. 15 Eylül ve sonrasında birden fazla uygun "
        "SAR adayı varsa post-sezon taze kanıt, sonra daha yeni sahne ve güçlü skor "
        "öncelenir; eski güçlü kanıt yalnız fallback olarak korunur. Yeni aday "
        "üretilmez; 150-249 m² MİKRO katmanı bu kurala girmez ve alarm/saha görevi açılmaz."
    )
    return report


def _write_if_changed(path, text):
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def update_sar_blind_priority():
    required = (
        CURRENT_BLIND_JSON,
        AUDIT_JSON,
        REPORT_JSON,
        GULBAHCE_SCAN_JSON,
        SAR_MAP_GEOJSON,
    )
    if any(not path.exists() for path in required):
        return False

    current = json.loads(CURRENT_BLIND_JSON.read_text(encoding="utf-8"))
    audit = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
    report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    scan = json.loads(GULBAHCE_SCAN_JSON.read_text(encoding="utf-8"))
    sar = json.loads(SAR_MAP_GEOJSON.read_text(encoding="utf-8"))
    guarded = apply_sar_blind_priority(current, audit, report, scan, sar)

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
        "rapor_tarihi": "2026-09-13",
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
                "mahalle": "Gülbahçe · güncel uydu kör alanı",
                "enlem": 38.346923,
                "boylam": 26.641132,
                "alan_m2": 1600,
                "neden": "BULUT",
                "alarm": False,
                "saha_gorevi": False,
            },
        ],
    }
    sar = {
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [26.642621, 38.34231],
                },
                "properties": {
                    "alan_m2": 400,
                    "alarm": False,
                    "saha_gorevi": False,
                    "arka_plan": False,
                    "bolge": "gulbahce",
                    "harita_katmani": "SAR_GUCLU_LOKAL",
                    "hedef_katmani": "ANA_250_PLUS",
                    "kaynak": "gulbahce_latest_state_blind_review.json",
                    "sar_lokal_degisim_skor_db": 3.045,
                    "sar_mekansal_ayrim": "KOMPAKT_LOKAL_DESTEKLI",
                    "sar_polarizasyon_uyumu": "CIFT_POL_GUCLU",
                    "temporal_durum": "ANI_YENI_LOKAL_BASLANGIC_DESTEKLI",
                    "yeni_tarih": "2026-09-12",
                },
            }
        ]
    }
    return current, audit, report, scan, sar


def _self_check():
    assert MIN_AREA_M2 == 250
    assert TARGET_MAX_AREA_M2 == 6500
    assert FULL_OPERATION_START == date(2026, 9, 15)
    current, audit, report, scan, sar = _fixtures()

    duty_day = date(2026, 9, 13)
    assert _is_duty_day(duty_day) is True
    guarded = apply_sar_blind_priority(
        current, audit, report, scan, sar, rotation_day=duty_day
    )
    meta = guarded["gulbahce_kor_alan_devriye_korumasi"]
    assert meta["sar_destekli_kor_onceligi_kullanildi"] is True
    assert meta["sar_destekli_kor_uygun_aday_sayisi"] == 1
    assert meta["sar_15eylul_taze_oncelik_aktif"] is False
    assert meta["secilen"]["alan_m2"] == 400
    assert meta["secilen"]["sar_lokal_degisim_skor_db"] == 3.045
    east = [
        item
        for item in guarded["kor_alan_saha_devriyesi"]
        if item.get("bolge_anahtari") == "east"
    ]
    assert len(east) == 1
    assert east[0]["gulbahce_sar_destekli_kor_onceligi"] is True
    assert east[0]["alarm"] is False and east[0]["saha_gorevi"] is False

    weak = json.loads(json.dumps(sar))
    weak["features"][0]["properties"]["sar_lokal_degisim_skor_db"] = 1.99
    weak_result = apply_sar_blind_priority(
        current, audit, report, scan, weak, rotation_day=duty_day
    )
    assert weak_result["gulbahce_kor_alan_devriye_korumasi"][
        "sar_destekli_kor_onceligi_kullanildi"
    ] is False
    assert weak_result["kor_alan_saha_devriyesi"][1]["alan_m2"] == 1600

    stale = json.loads(json.dumps(sar))
    stale["features"][0]["properties"]["yeni_tarih"] = "2026-09-08"
    stale_result = apply_sar_blind_priority(
        current, audit, report, scan, stale, rotation_day=duty_day
    )
    assert stale_result["gulbahce_kor_alan_devriye_korumasi"][
        "sar_destekli_kor_onceligi_kullanildi"
    ] is False

    micro = json.loads(json.dumps(current))
    micro["guncel_sahne_kor_kumeleri"][0]["alan_m2"] = 200
    micro_result = apply_sar_blind_priority(
        micro, audit, report, scan, sar, rotation_day=duty_day
    )
    assert micro_result["gulbahce_kor_alan_devriye_korumasi"][
        "sar_destekli_kor_uygun_aday_sayisi"
    ] == 0

    non_duty = apply_sar_blind_priority(
        current, audit, report, scan, sar, rotation_day=date(2026, 9, 12)
    )
    assert non_duty["gulbahce_kor_alan_devriye_korumasi"][
        "sar_destekli_kor_onceligi_kullanildi"
    ] is False

    # 15 Eylül sonrasında iki güvenli güçlü aday varsa daha yeni post-sezon kanıt,
    # daha yüksek skorlu sezon-öncesi kalibrasyon kanıtını geçer. Eski kanıt silinmez.
    postseason_sar = json.loads(json.dumps(sar))
    second = json.loads(json.dumps(postseason_sar["features"][0]))
    second["geometry"]["coordinates"] = [26.641132, 38.346923]
    second["properties"]["alan_m2"] = 1600
    second["properties"]["sar_lokal_degisim_skor_db"] = 2.2
    second["properties"]["yeni_tarih"] = "2026-09-15"
    postseason_sar["features"].append(second)
    postseason_day = date(2026, 9, 15)
    assert _is_duty_day(postseason_day) is True
    postseason_result = apply_sar_blind_priority(
        current,
        audit,
        report,
        scan,
        postseason_sar,
        rotation_day=postseason_day,
    )
    postseason_meta = postseason_result["gulbahce_kor_alan_devriye_korumasi"]
    assert postseason_meta["sar_15eylul_taze_oncelik_aktif"] is True
    assert postseason_meta["sar_15eylul_taze_uygun_aday_sayisi"] == 1
    assert postseason_meta["secilen"]["alan_m2"] == 1600
    assert postseason_meta["secilen"]["sar_yeni_tarih"] == "2026-09-15"
    assert postseason_meta["secilen"]["sar_15eylul_sonrasi"] is True

    postseason_fallback = apply_sar_blind_priority(
        current, audit, report, scan, sar, rotation_day=postseason_day
    )
    fallback_meta = postseason_fallback["gulbahce_kor_alan_devriye_korumasi"]
    assert fallback_meta["sar_15eylul_taze_uygun_aday_sayisi"] == 0
    assert fallback_meta["secilen"]["alan_m2"] == 400
    assert fallback_meta["secilen"]["sar_15eylul_sonrasi"] is False

    print("Gülbahçe SAR destekli güncel körlük öncelik öz testi başarılı.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if args.check_only:
        return
    changed = update_sar_blind_priority()
    print(
        "Gülbahçe SAR destekli körlük önceliği güncellendi."
        if changed
        else "Gülbahçe SAR destekli körlük önceliğinde değişiklik yok."
    )


if __name__ == "__main__":
    main()
