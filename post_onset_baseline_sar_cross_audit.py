"""13 Eylül sakin baseline'dan 17 Eylül sonrası müdahaleye benzeyen adayları SAR ile çaprazlar.

Yalnız diagnostiktir; alarm, saha görevi veya rota üretmez. Sentinel-2 10 m veriden
kazı derinliği ölçmez. Amaç, doğrulanmış kazı referansına benzeyen ve kalıcı bitki
negatif bağlamından geçmiş adayları zamansal olarak uygun Sentinel-1 lokal desteğiyle
ikinci kez kontrol etmektir. 250 m² ana eşik korunur; 150–249 m² yalnız MİKRO'dur.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path

BASE = Path(__file__).resolve().parent
BASELINE_JSON = BASE / "post_onset_baseline_vegetation_guard_review.json"
SAR_JSON = BASE / "foundation_excavation_sar_seed_review.json"
FEEDBACK_JSON = BASE / "manual_field_feedback.json"
OUTPUT_JSON = BASE / "post_onset_baseline_sar_cross_review.json"

MAIN_THRESHOLD_M2 = 250
MICRO_MIN_M2 = 150
MICRO_MAX_M2 = 249
MATCH_RADIUS_M = 45.0
MIN_MORPHOLOGY_SCORE = 65


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


def _region(region_key, baseline_region, sar_region, feedback):
    kept = [x for x in baseline_region.get("korunan_adaylar") or [] if isinstance(x, dict)]
    sar_rows = [x for x in sar_region.get("tum_sar_sonuclari") or [] if isinstance(x, dict)]
    scene_date = _date(baseline_region.get("post_onset_tarih"))
    rows = []
    for candidate in kept:
        sar, distance = _nearest(candidate, sar_rows)
        sar_match = bool(sar is not None and distance is not None and distance <= MATCH_RADIUS_M)
        sar_support = bool(
            sar_match
            and sar.get("sar_guclu_lokal_destek") is True
            and sar.get("sar_optik_doneme_zamansal_uygun") is True
        )
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
        blocked, feedback_row = _feedback_effect(candidate, scene_date, feedback)
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
            "sar_eslesme_mesafe_m": round(distance, 1) if distance is not None else None,
            "sar_guclu_lokal_destek": bool(sar_support),
            "sar_lokal_degisim_skor_db": sar.get("sar_lokal_degisim_skor_db") if sar else None,
            "sar_mekansal_ayrim": sar.get("sar_mekansal_ayrim") if sar else None,
            "geri_bildirim_engeli": blocked,
            "geri_bildirim": feedback_row,
            "coklu_kanit": multi,
            "ana_esik": main,
            "mikro_diagnostik": bool(multi and micro),
            "yuksek_guven_diagnostik": bool(multi and main),
            "alarm": False,
            "saha_gorevi": False,
        })
    rows.sort(key=lambda x: (x["yuksek_guven_diagnostik"], x["mikro_diagnostik"], x["morfoloji_puani"], x["spektral_etki_alani_m2"]), reverse=True)
    return {
        "durum": baseline_region.get("durum"),
        "bolge": baseline_region.get("bolge") or region_key,
        "baseline_tarih": baseline_region.get("baseline_tarih"),
        "post_onset_tarih": baseline_region.get("post_onset_tarih"),
        "sar_yeni_tarih": sar_region.get("sar_yeni_tarih"),
        "girdi_korunan_aday": len(kept),
        "sar_eslesen_aday": sum(1 for x in rows if x["sar_eslesme_mesafe_m"] is not None and x["sar_eslesme_mesafe_m"] <= MATCH_RADIUS_M),
        "coklu_kanit_sayisi": sum(1 for x in rows if x["coklu_kanit"]),
        "ana_esik_yuksek_guven_sayisi": sum(1 for x in rows if x["yuksek_guven_diagnostik"]),
        "mikro_coklu_kanit_sayisi": sum(1 for x in rows if x["mikro_diagnostik"]),
        "adaylar": rows,
    }


def audit(baseline=None, sar=None, feedback=None):
    baseline = baseline if isinstance(baseline, dict) else _load(BASELINE_JSON)
    sar = sar if isinstance(sar, dict) else _load(SAR_JSON)
    feedback = feedback if isinstance(feedback, dict) else _load(FEEDBACK_JSON)
    feedback_rows = [x for x in feedback.get("kayitlar") or [] if isinstance(x, dict)]
    regions = {}
    for key, baseline_region in (baseline.get("bolgeler") or {}).items():
        if not isinstance(baseline_region, dict):
            continue
        regions[key] = _region(key, baseline_region, (sar.get("bolgeler") or {}).get(key) or {}, feedback_rows)
    main_count = sum(int(x.get("ana_esik_yuksek_guven_sayisi") or 0) for x in regions.values())
    micro_count = sum(int(x.get("mikro_coklu_kanit_sayisi") or 0) for x in regions.values())
    return {
        "surum": 1,
        "amac": "13 Eylül sakin baseline ile 17 Eylül sonrası referans-benzeri temel/kepçe adaylarını güçlü lokal Sentinel-1 desteğiyle çaprazlamak",
        "gercek_derinlik_olcumu": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": [MICRO_MIN_M2, MICRO_MAX_M2],
        "sar_eslesme_yaricapi_m": MATCH_RADIUS_M,
        "toplam_ana_esik_yuksek_guven_diagnostik": main_count,
        "toplam_mikro_coklu_kanit": micro_count,
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "bolgeler": regions,
        "not": "Kalıcı bitki negatif bağlamından geçmiş baseline-referans adayları için bağımsız SAR pozitif desteği aranır. SAR yokluğu veto değildir; yalnız güçlü SAR varsa ikinci pozitif kanıt sayılır.",
    }


def _self_check():
    baseline = {"bolgeler": {"cesme": {"durum": "ok", "post_onset_tarih": "18.09.2026", "korunan_adaylar": [
        {"enlem": 38.30, "boylam": 26.30, "spektral_etki_alani_m2": 300, "seed_merkezli_morfoloji_puani": 90, "seed_merkezli_morfoloji_seviyesi": "YUKSEK"},
        {"enlem": 38.31, "boylam": 26.31, "spektral_etki_alani_m2": 200, "seed_merkezli_morfoloji_puani": 90, "seed_merkezli_morfoloji_seviyesi": "YUKSEK"},
    ]}}}
    sar = {"bolgeler": {"cesme": {"sar_yeni_tarih": "2026-09-18", "tum_sar_sonuclari": [
        {"enlem": 38.30, "boylam": 26.30, "sar_guclu_lokal_destek": True, "sar_optik_doneme_zamansal_uygun": True, "sar_lokal_degisim_skor_db": 2.4},
        {"enlem": 38.31, "boylam": 26.31, "sar_guclu_lokal_destek": True, "sar_optik_doneme_zamansal_uygun": True, "sar_lokal_degisim_skor_db": 2.2},
    ]}}}
    result = audit(baseline, sar, {"kayitlar": []})
    region = result["bolgeler"]["cesme"]
    assert region["ana_esik_yuksek_guven_sayisi"] == 1
    assert region["mikro_coklu_kanit_sayisi"] == 1
    assert result["alarm"] is False and result["saha_gorevi"] is False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("post-onset baseline SAR cross self-check: ok")
        return 0
    payload = audit()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
