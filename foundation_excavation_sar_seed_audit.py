"""Lokal Sentinel-2 kazı seedlerini Sentinel-1 RTC ile çapraz denetler.

Yalnız diagnostiktir; alarm, saha görevi veya rota üretmez. Sentinel-2 lokal seed
ve seed-merkezli morfoloji adaylarını, aynı koordinatta Sentinel-1 RTC
merkez-vs-çevre lokal kontrast değişimiyle kontrol eder. SAR sahnesi optik
13→15 Eylül değişiminden daha eskiyse bunu doğrulama saymaz.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import sentinel1_rtc_change_diagnostic as rtc


SEED_REVIEW = Path(__file__).with_name("seed_centered_excavation_morphology_review.json")
OUTPUT_JSON = Path(__file__).with_name("foundation_excavation_sar_seed_review.json")
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
LOCAL_SUPPORT = {"KOMPAKT_LOKAL_DESTEKLI", "LOKAL_AYRIM_DESTEKLI"}
STRONG_DB = 2.0
MAX_CANDIDATES_PER_REGION = 20


def _load_review():
    return json.loads(SEED_REVIEW.read_text(encoding="utf-8"))


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


def _sar_temporally_relevant(sar_new_date, optical_old_date):
    sar_date = _parse_date(sar_new_date)
    opt_date = _parse_date(optical_old_date)
    return bool(sar_date and opt_date and sar_date >= opt_date)


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


def _analyze_region(region_key, region):
    candidates = list(region.get("adaylar") or [])[:MAX_CANDIDATES_PER_REGION]
    targets = [_target_from_candidate(candidate) for candidate in candidates]
    result = rtc.inspect_region(region_key, targets)

    optical_old = region.get("onceki_tarih")
    sar_new = result.get("yeni_tarih")
    temporal_ok = _sar_temporally_relevant(sar_new, optical_old)

    rows = []
    for row in result.get("hedefler") or []:
        strong = _strong_local(row)
        cross = bool(strong and temporal_ok)
        pol_metrics = row.get("polarizasyon_metrikleri") or {}
        read_errors = row.get("okuma_hatalari") or {}
        rows.append({
            "enlem": row.get("enlem"),
            "boylam": row.get("boylam"),
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
            "sar_optik_doneme_zamansal_uygun": temporal_ok,
            "s2_sar_capraz_destek": cross,
            "alarm": False,
            "saha_gorevi": False,
        })

    rows.sort(
        key=lambda x: (
            bool(x["s2_sar_capraz_destek"]),
            float(x.get("sar_lokal_degisim_skor_db") or -1),
            float(x.get("s2_seed_morfoloji_puani") or 0),
        ),
        reverse=True,
    )
    cross_rows = [x for x in rows if x["s2_sar_capraz_destek"]]
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
        "hedef_sayisi": len(rows),
        "rtc_cifti_kapsayan_hedef": len(covered_rows),
        "sar_metrik_uretilen_hedef": len(metric_rows),
        "sar_okuma_hatasi_olan_hedef": len(error_rows),
        "s2_sar_capraz_destekli_sayi": len(cross_rows),
        "capraz_destekli_adaylar": cross_rows[:10],
        "tum_sar_sonuclari": rows,
        "alarm": False,
        "saha_gorevi": False,
    }


def _self_check():
    assert _parse_date("13.09.2026") == _parse_date("2026-09-13")
    assert _sar_temporally_relevant("2026-09-17", "13.09.2026")
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


def audit():
    _self_check()
    review = _load_review()
    regions = {}
    total_cross = 0
    for region_key in ("cesme", "uzunkuyu"):
        region = (review.get("bolgeler") or {}).get(region_key) or {}
        try:
            regions[region_key] = _analyze_region(region_key, region)
            total_cross += int(regions[region_key].get("s2_sar_capraz_destekli_sayi") or 0)
        except Exception as exc:
            regions[region_key] = {
                "bolge": region.get("bolge") or region_key,
                "durum": "HATA",
                "hata": f"{type(exc).__name__}: {exc}",
                "alarm": False,
                "saha_gorevi": False,
            }

    return {
        "surum": 2,
        "amac": "Lokal S2 temel/kepçe seedlerini zamansal olarak uygun Sentinel-1 RTC lokal desteğiyle çapraz denetlemek",
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "guclu_sar_esigi_db": STRONG_DB,
        "toplam_s2_sar_capraz_destekli": total_cross,
        "bolgeler": regions,
        "not": (
            "SAR yalnız optik değişim dönemine zamansal olarak yetişiyorsa çapraz destek sayılır. "
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
