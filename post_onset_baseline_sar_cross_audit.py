"""13 Eylül sakin baseline'dan 17 Eylül sonrası müdahaleye benzeyen adayları SAR ile çaprazlar.

Yalnız diagnostiktir; alarm, saha görevi veya rota üretmez. Sentinel-2 10 m veriden
kazı derinliği ölçmez. Amaç, doğrulanmış kazı referansına benzeyen ve kalıcı bitki
negatif bağlamından geçmiş adayları kendi koordinatlarında Sentinel-1 RTC ile ölçmektir.
Önceden hesaplanmış SAR hedef havuzuna yakınlık aranmaz; her baseline adayı exact hedef
olarak sorgulanır. 250 m² ana eşik korunur; 150–249 m² yalnız MİKRO'dur.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path

import sentinel1_rtc_change_diagnostic as rtc

BASE = Path(__file__).resolve().parent
BASELINE_JSON = BASE / "post_onset_baseline_vegetation_guard_review.json"
FEEDBACK_JSON = BASE / "manual_field_feedback.json"
OUTPUT_JSON = BASE / "post_onset_baseline_sar_cross_review.json"

MAIN_THRESHOLD_M2 = 250
MICRO_MIN_M2 = 150
MICRO_MAX_M2 = 249
MATCH_RADIUS_M = 45.0
MIN_MORPHOLOGY_SCORE = 65
STRONG_SAR_DB = 2.0
LOCAL_SUPPORT = {"KOMPAKT_LOKAL_DESTEKLI", "LOKAL_AYRIM_DESTEKLI"}


def _load(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _point(item):
    try:
        return float(item["enlem"]), float(item["boylam"])
    except (KeyError, TypeError, ValueError):
        return None


def _distance_m(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _nearest(target, records):
    tp = _point(target)
    if tp is None:
        return None, None
    best = None
    best_distance = None
    for row in records:
        rp = _point(row)
        if rp is None:
            continue
        distance = _distance_m(tp, rp)
        if best_distance is None or distance < best_distance:
            best = row
            best_distance = distance
    return best, best_distance


def _date(value):
    text = str(value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _feedback_effect(candidate, scene_date, feedback):
    cp = _point(candidate)
    if cp is None:
        return False, None
    matches = []
    for item in feedback:
        ip = _point(item)
        if ip is None:
            continue
        radius = max(float(item.get("eslesme_yaricapi_m") or 30), MATCH_RADIUS_M)
        distance = _distance_m(cp, ip)
        if distance <= radius:
            matches.append((distance, item))
    if not matches:
        return False, None
    distance, item = min(matches, key=lambda x: x[0])
    outcome = str(item.get("sonuc") or "").upper()
    result_date = _date(item.get("sonuc_tarihi"))
    same_or_older = bool(scene_date and result_date and scene_date <= result_date)
    blocked = outcome == "MEVCUT_MUSTERI" or (outcome == "YANLIS_POZITIF" and same_or_older)
    return blocked, {
        "id": item.get("id"),
        "sonuc": outcome,
        "mesafe_m": round(distance, 1),
        "sonuc_tarihi": item.get("sonuc_tarihi"),
        "ayni_veya_eski_sahne": same_or_older,
    }


def _strong_local(row):
    try:
        score = float(row.get("sar_lokal_degisim_skor_db"))
    except (TypeError, ValueError):
        return False
    return bool(score >= STRONG_SAR_DB and str(row.get("sar_mekansal_ayrim") or "") in LOCAL_SUPPORT)


def _brackets_onset(sar_old, sar_new, baseline_date, post_onset_date):
    old_date = _date(sar_old)
    new_date = _date(sar_new)
    baseline = _date(baseline_date)
    post_onset = _date(post_onset_date)
    return bool(old_date and new_date and baseline and post_onset and old_date <= baseline and new_date >= post_onset)


def _target(candidate):
    try:
        area = int(candidate.get("spektral_etki_alani_m2") or 0)
    except (TypeError, ValueError):
        area = 0
    return {
        "enlem": float(candidate["enlem"]),
        "boylam": float(candidate["boylam"]),
        "alan_m2": max(MAIN_THRESHOLD_M2, area),
        "kaynak": "BASELINE_REFERANS_BENZERI",
    }


def _evaluate_region(region_key, baseline_region, sar_result, feedback):
    kept = [x for x in baseline_region.get("korunan_adaylar") or [] if isinstance(x, dict) and _point(x)]
    sar_rows = [x for x in sar_result.get("hedefler") or [] if isinstance(x, dict) and _point(x)]
    post_scene_date = _date(baseline_region.get("post_onset_tarih"))
    pair_brackets = _brackets_onset(
        sar_result.get("eski_tarih"),
        sar_result.get("yeni_tarih"),
        baseline_region.get("baseline_tarih"),
        baseline_region.get("post_onset_tarih"),
    )

    rows = []
    for candidate in kept:
        sar, distance = _nearest(candidate, sar_rows)
        exact_sar = bool(sar is not None and distance is not None and distance <= MATCH_RADIUS_M)
        sar_support = bool(exact_sar and pair_brackets and _strong_local(sar))
        try:
            area = int(candidate.get("spektral_etki_alani_m2") or 0)
        except (TypeError, ValueError):
            area = 0
        try:
            morphology = float(candidate.get("seed_merkezli_morfoloji_puani") or 0)
        except (TypeError, ValueError):
            morphology = 0.0
        morphology_high = bool(
            morphology >= MIN_MORPHOLOGY_SCORE
            and str(candidate.get("seed_merkezli_morfoloji_seviyesi") or "").upper() == "YUKSEK"
        )
        blocked, feedback_row = _feedback_effect(candidate, post_scene_date, feedback)
        main = area >= MAIN_THRESHOLD_M2
        micro = MICRO_MIN_M2 <= area <= MICRO_MAX_M2
        multi = bool(morphology_high and sar_support and not blocked)
        rows.append({
            "enlem": candidate.get("enlem"),
            "boylam": candidate.get("boylam"),
            "spektral_etki_alani_m2": area,
            "morfoloji_puani": morphology,
            "morfoloji_yuksek": morphology_high,
            "kalici_bitki_negatif_baglami": False,
            "sar_exact_hedef_mesafe_m": round(distance, 1) if distance is not None else None,
            "sar_cifti_baseline_onseti_cevreliyor": pair_brackets,
            "sar_guclu_lokal_destek": sar_support,
            "sar_lokal_degisim_skor_db": sar.get("sar_lokal_degisim_skor_db") if sar else None,
            "sar_polarizasyon_uyumu": sar.get("sar_polarizasyon_uyumu") if sar else None,
            "sar_mekansal_ayrim": sar.get("sar_mekansal_ayrim") if sar else None,
            "sar_cevre_degisim_tepe_db": sar.get("sar_cevre_degisim_tepe_db") if sar else None,
            "geri_bildirim_engeli": blocked,
            "geri_bildirim": feedback_row,
            "coklu_kanit": multi,
            "ana_esik": main,
            "mikro_diagnostik": bool(multi and micro),
            "yuksek_guven_diagnostik": bool(multi and main),
            "alarm": False,
            "saha_gorevi": False,
        })

    rows.sort(
        key=lambda x: (
            x["yuksek_guven_diagnostik"],
            x["mikro_diagnostik"],
            x["sar_guclu_lokal_destek"],
            x["morfoloji_puani"],
            x["spektral_etki_alani_m2"],
        ),
        reverse=True,
    )
    return {
        "durum": sar_result.get("durum") or baseline_region.get("durum"),
        "bolge": baseline_region.get("bolge") or region_key,
        "baseline_tarih": baseline_region.get("baseline_tarih"),
        "post_onset_tarih": baseline_region.get("post_onset_tarih"),
        "sar_eski_tarih": sar_result.get("eski_tarih"),
        "sar_yeni_tarih": sar_result.get("yeni_tarih"),
        "sar_cifti_baseline_onseti_cevreliyor": pair_brackets,
        "girdi_korunan_aday": len(kept),
        "sar_exact_hedef_sayisi": sum(1 for x in rows if x["sar_exact_hedef_mesafe_m"] is not None and x["sar_exact_hedef_mesafe_m"] <= MATCH_RADIUS_M),
        "coklu_kanit_sayisi": sum(1 for x in rows if x["coklu_kanit"]),
        "ana_esik_yuksek_guven_sayisi": sum(1 for x in rows if x["yuksek_guven_diagnostik"]),
        "mikro_coklu_kanit_sayisi": sum(1 for x in rows if x["mikro_diagnostik"]),
        "adaylar": rows,
    }


def _region(region_key, baseline_region, feedback):
    kept = [x for x in baseline_region.get("korunan_adaylar") or [] if isinstance(x, dict) and _point(x)]
    if not kept:
        return {
            "durum": "aday_yok",
            "bolge": baseline_region.get("bolge") or region_key,
            "baseline_tarih": baseline_region.get("baseline_tarih"),
            "post_onset_tarih": baseline_region.get("post_onset_tarih"),
            "girdi_korunan_aday": 0,
            "sar_exact_hedef_sayisi": 0,
            "coklu_kanit_sayisi": 0,
            "ana_esik_yuksek_guven_sayisi": 0,
            "mikro_coklu_kanit_sayisi": 0,
            "adaylar": [],
        }
    result = rtc.inspect_region(region_key, [_target(x) for x in kept])
    return _evaluate_region(region_key, baseline_region, result, feedback)


def audit(baseline=None, feedback=None):
    baseline = baseline if isinstance(baseline, dict) else _load(BASELINE_JSON)
    feedback = feedback if isinstance(feedback, dict) else _load(FEEDBACK_JSON)
    feedback_rows = [x for x in feedback.get("kayitlar") or [] if isinstance(x, dict)]
    regions = {}
    errors = []
    for key, baseline_region in (baseline.get("bolgeler") or {}).items():
        if not isinstance(baseline_region, dict):
            continue
        try:
            regions[key] = _region(key, baseline_region, feedback_rows)
        except Exception as exc:
            errors.append({"bolge_anahtari": key, "hata": f"{type(exc).__name__}: {exc}"})
            regions[key] = {
                "durum": "hata",
                "bolge": baseline_region.get("bolge") or key,
                "hata": f"{type(exc).__name__}: {exc}",
                "ana_esik_yuksek_guven_sayisi": 0,
                "mikro_coklu_kanit_sayisi": 0,
                "adaylar": [],
            }
    main_count = sum(int(x.get("ana_esik_yuksek_guven_sayisi") or 0) for x in regions.values())
    micro_count = sum(int(x.get("mikro_coklu_kanit_sayisi") or 0) for x in regions.values())
    return {
        "surum": 2,
        "amac": "13 Eylül sakin baseline ile 17 Eylül sonrası referans-benzeri temel/kepçe adaylarını kendi koordinatlarında Sentinel-1 RTC ile çaprazlamak",
        "gercek_derinlik_olcumu": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": [MICRO_MIN_M2, MICRO_MAX_M2],
        "sar_guclu_lokal_esik_db": STRONG_SAR_DB,
        "toplam_ana_esik_yuksek_guven_diagnostik": main_count,
        "toplam_mikro_coklu_kanit": micro_count,
        "bolge_hata_sayisi": len(errors),
        "bolge_hatalari": errors,
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "bolgeler": regions,
        "not": "Baseline-referans adayları SAR'ın önceden seçilmiş hedef havuzuna bağımlı değildir; exact koordinatlarında ölçülür. SAR yokluğu/zayıflığı veto değildir, yalnız güçlü lokal ve onset penceresini çevreleyen SAR ikinci pozitif kanıt sayılır.",
    }


def _self_check():
    assert _strong_local({"sar_lokal_degisim_skor_db": 2.1, "sar_mekansal_ayrim": "KOMPAKT_LOKAL_DESTEKLI"})
    assert not _strong_local({"sar_lokal_degisim_skor_db": 1.9, "sar_mekansal_ayrim": "KOMPAKT_LOKAL_DESTEKLI"})
    assert _brackets_onset("2026-09-12", "2026-09-18", "13.09.2026", "18.09.2026")
    assert not _brackets_onset("2026-09-16", "2026-09-18", "13.09.2026", "18.09.2026")

    baseline_region = {
        "durum": "ok",
        "bolge": "Çeşme",
        "baseline_tarih": "13.09.2026",
        "post_onset_tarih": "18.09.2026",
        "korunan_adaylar": [
            {"enlem": 38.30, "boylam": 26.30, "spektral_etki_alani_m2": 300, "seed_merkezli_morfoloji_puani": 90, "seed_merkezli_morfoloji_seviyesi": "YUKSEK"},
            {"enlem": 38.31, "boylam": 26.31, "spektral_etki_alani_m2": 200, "seed_merkezli_morfoloji_puani": 90, "seed_merkezli_morfoloji_seviyesi": "YUKSEK"},
        ],
    }
    sar_result = {
        "durum": "hazir",
        "eski_tarih": "2026-09-12",
        "yeni_tarih": "2026-09-18",
        "hedefler": [
            {"enlem": 38.30, "boylam": 26.30, "sar_lokal_degisim_skor_db": 2.4, "sar_mekansal_ayrim": "KOMPAKT_LOKAL_DESTEKLI"},
            {"enlem": 38.31, "boylam": 26.31, "sar_lokal_degisim_skor_db": 2.2, "sar_mekansal_ayrim": "LOKAL_AYRIM_DESTEKLI"},
        ],
    }
    result = _evaluate_region("cesme", baseline_region, sar_result, [])
    assert result["sar_exact_hedef_sayisi"] == 2
    assert result["ana_esik_yuksek_guven_sayisi"] == 1
    assert result["mikro_coklu_kanit_sayisi"] == 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("post-onset baseline exact-SAR cross self-check: ok")
        return 0
    payload = audit()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
