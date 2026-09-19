"""13 Eylül sakin baseline → 17 Eylül sonrası S2 adaylarını Sentinel-1 ile çaprazlar.

Bu katman yalnız diagnostiktir. Alarm, saha görevi veya rota üretmez. Amaç,
post_onset_baseline_excavation_audit tarafından gerçek kazı referansına benzer bulunan
250 m²+ adayların seed-merkezli 15→18 havuzuna girmedikleri için SAR katmanında
kaybolmasını önlemektir.

Sentinel-1 güçlü lokal destek POZİTİF bağımsız kanıttır; zayıf/yok SAR hiçbir zaman
negatif veto değildir. Sentinel-2 10 m veriden gerçek kazı derinliği/alanı ölçülmez;
``spektral_etki_alani_m2`` yalnız piksel ölçekli diagnostik etkidir.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path

import sentinel1_rtc_change_diagnostic as rtc


BASE = Path(__file__).resolve().parent
BASELINE_REVIEW = BASE / "post_onset_baseline_excavation_review.json"
FEEDBACK_JSON = BASE / "manual_field_feedback.json"
OUTPUT_JSON = BASE / "post_onset_baseline_sar_bridge_review.json"

MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
MAX_TARGETS_PER_REGION = 20
STRONG_DB = 2.0
LOCAL_SUPPORT = {"KOMPAKT_LOKAL_DESTEKLI", "LOKAL_AYRIM_DESTEKLI"}
DEFAULT_FEEDBACK_RADIUS_M = 30.0

# Tarla/bahçe bağlamı için konservatif negatif işaretler. Bunlar tek başına
# üretim filtresi değildir; yalnız bu diagnostikte pozitif SAR desteğinin
# "temel/kepçe lehine çoklu kanıt" diye sayılmasını engeller.
MAX_PERSISTENT_VEG_FRACTION = 0.35
MIN_BARE_FRACTION = 0.55
MAX_SOIL_FRACTION = 0.25


def _load(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_date(value):
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _distance_m(a, b):
    lat1, lon1 = map(float, a)
    lat2, lon2 = map(float, b)
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _feedback_rows():
    payload = _load(FEEDBACK_JSON)
    return [item for item in payload.get("kayitlar", []) if isinstance(item, dict)]


def _hard_feedback_block(candidate, feedback):
    point = (_number(candidate.get("enlem")), _number(candidate.get("boylam")))
    matches = []
    for item in feedback:
        result = str(item.get("sonuc") or "").upper()
        if result not in {"YANLIS_POZITIF", "MEVCUT_MUSTERI"}:
            continue
        try:
            other = (float(item["enlem"]), float(item["boylam"]))
        except (KeyError, TypeError, ValueError):
            continue
        radius = max(_number(item.get("eslesme_yaricapi_m"), DEFAULT_FEEDBACK_RADIUS_M), 1.0)
        distance = _distance_m(point, other)
        if distance <= radius:
            matches.append({
                "id": item.get("id"),
                "sonuc": result,
                "mesafe_m": round(distance, 1),
                "yaricap_m": round(radius, 1),
            })
    matches.sort(key=lambda x: x["mesafe_m"])
    return matches[0] if matches else None


def _agri_negative(candidate):
    persistent = _number(candidate.get("kalici_bitki_orani_5x5"))
    bare = _number(candidate.get("ciplak_zemin_orani_5x5"))
    soil = _number(candidate.get("soil_mask_orani_5x5"))
    reasons = []
    if persistent >= MAX_PERSISTENT_VEG_FRACTION:
        reasons.append("kalici_bitki_yuksek")
    if bare < MIN_BARE_FRACTION:
        reasons.append("ciplak_zemin_yetersiz")
    if soil >= MAX_SOIL_FRACTION:
        reasons.append("genis_toprak_sinyali")
    return reasons


def _strong_sar(row):
    return bool(
        _number(row.get("sar_lokal_degisim_skor_db"), -999) >= STRONG_DB
        and str(row.get("sar_mekansal_ayrim") or "") in LOCAL_SUPPORT
    )


def _targets(region_key, region, feedback):
    rows = []
    for candidate in region.get("benzer_diagnostik_adaylar", []):
        if not isinstance(candidate, dict):
            continue
        effect = int(_number(candidate.get("spektral_etki_alani_m2")))
        if effect < MAIN_THRESHOLD_M2:
            continue
        feedback_block = _hard_feedback_block(candidate, feedback)
        agri_reasons = _agri_negative(candidate)
        rows.append({
            "enlem": _number(candidate.get("enlem")),
            "boylam": _number(candidate.get("boylam")),
            "alan_m2": effect,
            "kaynak": "S2_13_BASELINE_POST_ONSET_REFERANS_BENZERI",
            "bolge_anahtari": region_key,
            "baseline_tarih": region.get("baseline_tarih"),
            "post_onset_tarih": region.get("post_onset_tarih"),
            "s2_spektral_etki_alani_m2": effect,
            "s2_seed_sinifi": candidate.get("seed_sinifi"),
            "s2_seed_morfoloji_puani": candidate.get("seed_merkezli_morfoloji_puani"),
            "s2_seed_morfoloji_seviyesi": candidate.get("seed_merkezli_morfoloji_seviyesi"),
            "kalici_bitki_orani_5x5": candidate.get("kalici_bitki_orani_5x5"),
            "ciplak_zemin_orani_5x5": candidate.get("ciplak_zemin_orani_5x5"),
            "soil_mask_orani_5x5": candidate.get("soil_mask_orani_5x5"),
            "saha_negatif_eslesme": feedback_block,
            "tarla_bahce_negatif_nedenler": agri_reasons,
        })
    rows.sort(
        key=lambda x: (
            bool(x["saha_negatif_eslesme"]),
            bool(x["tarla_bahce_negatif_nedenler"]),
            -_number(x.get("s2_seed_morfoloji_puani")),
            -int(x.get("s2_spektral_etki_alani_m2") or 0),
        )
    )
    return rows[:MAX_TARGETS_PER_REGION]


def _analyze_region(region_key, region, feedback):
    targets = _targets(region_key, region, feedback)
    if not targets:
        return {
            "durum": "ADAY_YOK",
            "baseline_tarih": region.get("baseline_tarih"),
            "post_onset_tarih": region.get("post_onset_tarih"),
            "hedef_sayisi": 0,
            "pozitif_sar_destekli_sayi": 0,
            "adaylar": [],
        }

    result = rtc.inspect_region(region_key, targets)
    sar_new = _parse_date(result.get("yeni_tarih"))
    post_date = _parse_date(region.get("post_onset_tarih"))
    temporal_ok = bool(sar_new and post_date and sar_new >= post_date)

    rows = []
    for row in result.get("hedefler") or []:
        feedback_block = row.get("saha_negatif_eslesme")
        agri_reasons = list(row.get("tarla_bahce_negatif_nedenler") or [])
        strong = _strong_sar(row)
        positive = bool(strong and temporal_ok and not feedback_block and not agri_reasons)
        rows.append({
            "enlem": row.get("enlem"),
            "boylam": row.get("boylam"),
            "s2_spektral_etki_alani_m2": row.get("s2_spektral_etki_alani_m2"),
            "s2_seed_sinifi": row.get("s2_seed_sinifi"),
            "s2_seed_morfoloji_puani": row.get("s2_seed_morfoloji_puani"),
            "s2_seed_morfoloji_seviyesi": row.get("s2_seed_morfoloji_seviyesi"),
            "kalici_bitki_orani_5x5": row.get("kalici_bitki_orani_5x5"),
            "ciplak_zemin_orani_5x5": row.get("ciplak_zemin_orani_5x5"),
            "soil_mask_orani_5x5": row.get("soil_mask_orani_5x5"),
            "saha_negatif_eslesme": feedback_block,
            "tarla_bahce_negatif_nedenler": agri_reasons,
            "sar_eski_tarih": result.get("eski_tarih"),
            "sar_yeni_tarih": result.get("yeni_tarih"),
            "sar_post_onset_zamansal_uygun": temporal_ok,
            "sar_lokal_degisim_skor_db": row.get("sar_lokal_degisim_skor_db"),
            "sar_polarizasyon_uyumu": row.get("sar_polarizasyon_uyumu"),
            "sar_mekansal_ayrim": row.get("sar_mekansal_ayrim"),
            "sar_cevre_degisim_tepe_db": row.get("sar_cevre_degisim_tepe_db"),
            "sar_guclu_lokal_destek": strong,
            "baseline_sar_pozitif_destek": positive,
            "alarm": False,
            "saha_gorevi": False,
        })
    rows.sort(
        key=lambda x: (
            bool(x["baseline_sar_pozitif_destek"]),
            _number(x.get("sar_lokal_degisim_skor_db"), -999),
            _number(x.get("s2_seed_morfoloji_puani")),
        ),
        reverse=True,
    )
    supported = [x for x in rows if x["baseline_sar_pozitif_destek"]]
    return {
        "durum": result.get("durum"),
        "baseline_tarih": region.get("baseline_tarih"),
        "post_onset_tarih": region.get("post_onset_tarih"),
        "sar_eski_tarih": result.get("eski_tarih"),
        "sar_yeni_tarih": result.get("yeni_tarih"),
        "hedef_sayisi": len(rows),
        "pozitif_sar_destekli_sayi": len(supported),
        "pozitif_sar_destekli_adaylar": supported,
        "adaylar": rows,
    }


def _self_check():
    tp_like = {
        "kalici_bitki_orani_5x5": 0.0,
        "ciplak_zemin_orani_5x5": 0.92,
        "soil_mask_orani_5x5": 0.08,
    }
    field_like = {
        "kalici_bitki_orani_5x5": 0.40,
        "ciplak_zemin_orani_5x5": 0.52,
        "soil_mask_orani_5x5": 0.0,
    }
    broad_soil = {
        "kalici_bitki_orani_5x5": 0.0,
        "ciplak_zemin_orani_5x5": 0.92,
        "soil_mask_orani_5x5": 0.68,
    }
    assert _agri_negative(tp_like) == []
    assert "kalici_bitki_yuksek" in _agri_negative(field_like)
    assert "ciplak_zemin_yetersiz" in _agri_negative(field_like)
    assert "genis_toprak_sinyali" in _agri_negative(broad_soil)
    assert _strong_sar({
        "sar_lokal_degisim_skor_db": 2.1,
        "sar_mekansal_ayrim": "KOMPAKT_LOKAL_DESTEKLI",
    })
    assert not _strong_sar({
        "sar_lokal_degisim_skor_db": 3.0,
        "sar_mekansal_ayrim": "KARISIK_DUSUK_LOKALLIK",
    })
    assert _parse_date("18.09.2026") == _parse_date("2026-09-18")


def audit():
    _self_check()
    baseline = _load(BASELINE_REVIEW)
    feedback = _feedback_rows()
    regions = {}
    total_supported = 0
    for region_key in ("cesme", "uzunkuyu"):
        region = (baseline.get("bolgeler") or {}).get(region_key) or {}
        try:
            regions[region_key] = _analyze_region(region_key, region, feedback)
            total_supported += int(regions[region_key].get("pozitif_sar_destekli_sayi") or 0)
        except Exception as exc:
            regions[region_key] = {
                "durum": "HATA",
                "hata": f"{type(exc).__name__}: {exc}",
                "alarm": False,
                "saha_gorevi": False,
            }
    return {
        "surum": 1,
        "amac": "13 Eylül baseline→17 Eylül sonrası referans-benzeri 250 m²+ S2 adaylarını Sentinel-1 pozitif lokal destekle çaprazlamak",
        "gercek_derinlik_olcumu": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "guclu_sar_esigi_db": STRONG_DB,
        "sar_yoklugu_negatif_veto": False,
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "toplam_pozitif_sar_destekli": total_supported,
        "bolgeler": regions,
        "not": (
            "Bu köprü yalnız diagnostiktir. 250 m² altı adayları ana listeye taşımaz; "
            "SAR zayıflığını negatif veto saymaz. Saha yanlış-pozitif/mevcut-müşteri ve "
            "belirgin tarla-bahçe bağlamı pozitif çoklu-kanıt sayımını engeller."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("post-onset baseline SAR bridge self-check: ok")
        return 0
    payload = audit()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
