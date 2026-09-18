"""Lokal Sentinel-2 kazı seedlerini Sentinel-1 RTC ile çapraz denetler.

Yalnız diagnostiktir; alarm, saha görevi veya rota üretmez. Sentinel-2 lokal seed
ve seed-merkezli morfoloji adaylarını, aynı koordinatta Sentinel-1 RTC
merkez-vs-çevre lokal kontrast değişimiyle kontrol eder. SAR sahnesi optik
13→15 Eylül değişiminden daha eskiyse bunu doğrulama saymaz.

Saha tarafından doğrulanmış kazı koordinatları ayrıca tam koordinatlarıyla bir
kalibrasyon hedefi olarak ölçülür. Bu hedefler S2-SAR çapraz desteği sayılmaz;
yalnız doğrulama tarihinden sonra gelen SAR sahnesindeki lokal imzayı kaydeder.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import sentinel1_rtc_change_diagnostic as rtc


SEED_REVIEW = Path(__file__).with_name("seed_centered_excavation_morphology_review.json")
FEEDBACK_JSON = Path(__file__).with_name("manual_field_feedback.json")
OUTPUT_JSON = Path(__file__).with_name("foundation_excavation_sar_seed_review.json")
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
LOCAL_SUPPORT = {"KOMPAKT_LOKAL_DESTEKLI", "LOKAL_AYRIM_DESTEKLI"}
STRONG_DB = 2.0
# Seed-merkezli katman normalde ilk 20 morfoloji adayını, ayrıca en fazla 20
# referans-çekirdeği öncelikli adayı dışarı verir. SAR denetimi bu ikinci grubu
# yeniden kesmemeli; 40 hedef runtime'ı sınırlarken tüm diagnostik havuzu korur.
MAX_CANDIDATES_PER_REGION = 40
CALIBRATION_SOURCE = "SAHA_DOGRULANMIS_KAZI_KALIBRASYON"


def _load_review():
    return json.loads(SEED_REVIEW.read_text(encoding="utf-8"))


def _load_feedback():
    try:
        payload = json.loads(FEEDBACK_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    return [x for x in payload.get("kayitlar", []) if isinstance(x, dict)]


def _parse_date(value):
    """Repo içindeki iki tarih biçimini aynı şekilde karşılaştırılabilir yap."""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    return None


def _sar_temporally_relevant(sar_new_date, reference_date):
    sar_date = _parse_date(sar_new_date)
    ref_date = _parse_date(reference_date)
    return bool(sar_date and ref_date and sar_date >= ref_date)


def _strong_local(row):
    try:
        score = float(row.get("sar_lokal_degisim_skor_db"))
    except (TypeError, ValueError):
        return False
    return bool(score >= STRONG_DB and str(row.get("sar_mekansal_ayrim") or "") in LOCAL_SUPPORT)


def _target_from_candidate(candidate):
    effect = int(candidate.get("spektral_etki_alani_m2") or 0)
    return {
        "enlem": float(candidate["enlem"]),
        "boylam": float(candidate["boylam"]),
        "alan_m2": max(MAIN_THRESHOLD_M2, effect),
        "kaynak": "S2_LOKAL_SEED_MORFOLOJI",
        "s2_seed_morfoloji_puani": candidate.get("seed_merkezli_morfoloji_puani"),
        "s2_seed_morfoloji_seviyesi": candidate.get("seed_merkezli_morfoloji_seviyesi"),
        "s2_spektral_etki_alani_m2": effect,
    }


def _confirmed_calibration_targets(region_key):
    """Doğrulanmış kazıyı exact saha koordinatında yalnız diagnostik hedef yap."""
    aoi = rtc.s1.AOIS[region_key]
    west, south, east, north = aoi["bbox"]
    targets = []
    for item in _load_feedback():
        if str(item.get("sonuc") or "").upper() != "DOGRULANMIS_KAZI":
            continue
        try:
            lat = float(item["enlem"])
            lon = float(item["boylam"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (south <= lat <= north and west <= lon <= east):
            continue
        try:
            area = int(item.get("yaklasik_alan_m2") or MAIN_THRESHOLD_M2)
        except (TypeError, ValueError):
            area = MAIN_THRESHOLD_M2
        targets.append({
            "enlem": lat,
            "boylam": lon,
            "alan_m2": max(MAIN_THRESHOLD_M2, area),
            "kaynak": CALIBRATION_SOURCE,
            "kalibrasyon_id": item.get("id"),
            "saha_dogrulama_tarihi": item.get("sonuc_tarihi"),
            "s2_seed_morfoloji_puani": None,
            "s2_seed_morfoloji_seviyesi": None,
            "s2_spektral_etki_alani_m2": None,
        })
    return targets


def _analyze_region(region_key, region):
    candidates = list(region.get("adaylar") or [])[:MAX_CANDIDATES_PER_REGION]
    seed_targets = [_target_from_candidate(candidate) for candidate in candidates]
    calibration_targets = _confirmed_calibration_targets(region_key)
    targets = seed_targets + calibration_targets
    result = rtc.inspect_region(region_key, targets)

    optical_old = region.get("onceki_tarih")
    sar_new = result.get("yeni_tarih")
    temporal_ok = _sar_temporally_relevant(sar_new, optical_old)

    rows = []
    for row in result.get("hedefler") or []:
        strong = _strong_local(row)
        source = str(row.get("kaynak") or "")
        is_calibration = source == CALIBRATION_SOURCE
        calibration_temporal_ok = bool(
            is_calibration
            and _sar_temporally_relevant(sar_new, row.get("saha_dogrulama_tarihi"))
        )
        cross = bool(strong and temporal_ok and not is_calibration)
        calibration_support = bool(strong and calibration_temporal_ok)
        pol_metrics = row.get("polarizasyon_metrikleri") or {}
        read_errors = row.get("okuma_hatalari") or {}
        rows.append({
            "enlem": row.get("enlem"),
            "boylam": row.get("boylam"),
            "kaynak": source or None,
            "kalibrasyon_id": row.get("kalibrasyon_id"),
            "saha_dogrulama_tarihi": row.get("saha_dogrulama_tarihi"),
            "s2_seed_morfoloji_puani": row.get("s2_seed_morfoloji_puani"),
            "s2_seed_morfoloji_seviyesi": row.get("s2_seed_morfoloji_seviyesi"),
            "rtc_cifti_kapsiyor": row.get("rtc_cifti_kapsiyor"),
            "sar_degisim_durumu": row.get("sar_degisim_durumu"),
            "sar_metrik_var": bool(pol_metrics),
            "sar_okuma_hatalari": read_errors or None,
            "sar_lokal_degisim_skor_db": row.get("sar_lokal_degisim_skor_db"),
            "sar_polarizasyon_uyumu": row.get("sar_polarizasyon_uyumu"),
            "sar_mekansal_ayrim": row.get("sar_mekansal_ayrim"),
            "sar_cevre_degisim_tepe_db": row.get("sar_cevre_degisim_tepe_db"),
            "sar_guclu_lokal_destek": strong,
            "sar_optik_doneme_zamansal_uygun": temporal_ok if not is_calibration else None,
            "sar_saha_dogrulama_sonrasi": calibration_temporal_ok if is_calibration else None,
            "s2_sar_capraz_destek": cross,
            "sar_kalibrasyon_destegi": calibration_support,
            "alarm": False,
            "saha_gorevi": False,
        })

    rows.sort(
        key=lambda x: (
            bool(x["sar_kalibrasyon_destegi"]),
            bool(x["s2_sar_capraz_destek"]),
            float(x.get("sar_lokal_degisim_skor_db") or -1),
            float(x.get("s2_seed_morfoloji_puani") or 0),
        ),
        reverse=True,
    )
    cross_rows = [x for x in rows if x["s2_sar_capraz_destek"]]
    calibration_rows = [x for x in rows if x.get("kaynak") == CALIBRATION_SOURCE]
    supported_calibration_rows = [x for x in calibration_rows if x["sar_kalibrasyon_destegi"]]
    covered_rows = [x for x in rows if x.get("rtc_cifti_kapsiyor") is True]
    metric_rows = [x for x in rows if x.get("sar_metrik_var") is True]
    error_rows = [x for x in rows if x.get("sar_okuma_hatalari")]
    return {
        "bolge": region.get("bolge") or region_key,
        "durum": result.get("durum"),
        "s2_onceki_tarih": optical_old,
        "s2_son_tarih": region.get("son_tarih"),
        "sar_eski_tarih": result.get("eski_tarih"),
        "sar_yeni_tarih": sar_new,
        "sar_optik_doneme_zamansal_uygun": temporal_ok,
        "s2_seed_hedef_sayisi": len(seed_targets),
        "saha_kalibrasyon_hedef_sayisi": len(calibration_targets),
        "hedef_sayisi": len(rows),
        "rtc_cifti_kapsayan_hedef": len(covered_rows),
        "sar_metrik_uretilen_hedef": len(metric_rows),
        "sar_okuma_hatasi_olan_hedef": len(error_rows),
        "s2_sar_capraz_destekli_sayi": len(cross_rows),
        "saha_kalibrasyon_sar_destekli_sayi": len(supported_calibration_rows),
        "saha_kalibrasyon_sonuclari": calibration_rows,
        "capraz_destekli_adaylar": cross_rows[:10],
        "tum_sar_sonuclari": rows,
        "alarm": False,
        "saha_gorevi": False,
    }


def _self_check():
    assert _parse_date("13.09.2026") == _parse_date("2026-09-13")
    assert _sar_temporally_relevant("2026-09-17", "13.09.2026")
    assert _sar_temporally_relevant("2026-09-17", "2026-09-16")
    assert _sar_temporally_relevant("2026-09-15", "2026-09-13")
    assert not _sar_temporally_relevant("2026-09-12", "13.09.2026")
    assert _strong_local({
        "sar_lokal_degisim_skor_db": 2.2,
        "sar_mekansal_ayrim": "KOMPAKT_LOKAL_DESTEKLI",
    })
    assert not _strong_local({
        "sar_lokal_degisim_skor_db": 2.2,
        "sar_mekansal_ayrim": "GENIS_CEVRE_DEGISIMI_ESLIK_EDIYOR",
    })
    assert MAX_CANDIDATES_PER_REGION >= 40


def audit():
    _self_check()
    review = _load_review()
    regions = {}
    total_cross = 0
    total_calibration_support = 0
    for region_key in ("cesme", "uzunkuyu"):
        region = (review.get("bolgeler") or {}).get(region_key) or {}
        try:
            regions[region_key] = _analyze_region(region_key, region)
            total_cross += int(regions[region_key].get("s2_sar_capraz_destekli_sayi") or 0)
            total_calibration_support += int(
                regions[region_key].get("saha_kalibrasyon_sar_destekli_sayi") or 0
            )
        except Exception as exc:
            regions[region_key] = {
                "bolge": region.get("bolge") or region_key,
                "durum": "HATA",
                "hata": f"{type(exc).__name__}: {exc}",
                "alarm": False,
                "saha_gorevi": False,
            }

    return {
        "surum": 3,
        "amac": "Lokal S2 temel/kepçe seedlerini ve saha doğrulanmış kazı kalibrasyon hedeflerini zamansal olarak uygun Sentinel-1 RTC lokal desteğiyle çapraz denetlemek",
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "guclu_sar_esigi_db": STRONG_DB,
        "toplam_s2_sar_capraz_destekli": total_cross,
        "toplam_saha_kalibrasyon_sar_destekli": total_calibration_support,
        "bolgeler": regions,
        "not": (
            "SAR yalnız optik değişim dönemine zamansal olarak yetişiyorsa S2-SAR çapraz destek sayılır. "
            "Saha doğrulanmış kazı exact koordinatında ayrıca diagnostik ölçülür; bu hedef ancak SAR yeni sahnesi saha doğrulama tarihine eşit/yeni ise kalibrasyon desteği sayılır ve S2-SAR aday sayısına katılmaz. "
            "Tarih karşılaştırması YYYY-MM-DD ve DD.MM.YYYY biçimlerini birlikte destekler. "
            "Bu katman tek başına saha görevi üretmez; saha kalibrasyonu olmadan rota kapısına bağlanmaz."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("foundation excavation SAR seed self-check: ok")
        return
    payload = audit()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
