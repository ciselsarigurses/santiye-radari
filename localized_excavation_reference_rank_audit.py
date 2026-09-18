"""Düşük-kontrast lokal kazı seedlerini çekirdek saha referanslarına göre sıralar.

Bu katman yalnız diagnostiktir. Alarm, saha görevi veya rota üretmez; 250 m² ana
üretim eşiğini ve 150–249 m² MİKRO politikasını değiştirmez. Amaç 15→18 Eylül
Sentinel-2 penceresinde doğrulanmış gerçek kazının düşük-kontrast imzasına benzeyen
adayların, daha parlak lokal değişimler yüzünden diagnostik kısa listede geriye
düşmesini engellemek ve kullanıcının verdiği dört tarla/bahçe yanlış pozitifiyle
ayrımı ölçmektir.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import excavation_reference_signature_audit as signature
import localized_excavation_seed_audit as localized
import satellite


OUTPUT_JSON = Path(__file__).with_name("localized_excavation_reference_rank_review.json")
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
CORE_POSITIVE_ID = "FN-20260916-CESME-001"
CORE_NEGATIVE_IDS = {
    "FP-20260916-REISDERE-001",
    "FP-20260917-UZUNKUYU-001",
    "FP-20260917-MUSALLA-001",
    "FP-20260917-MUSALLA-002",
}
# Pozitif çekirdek yarıçapı, doğrulanmış kazı ile en yakın çekirdek yanlış pozitif
# arasındaki imza mesafesinin yarısıdır. Bu, iki referansın orta düzleminden önce
# kalan konservatif bir diagnostik komşuluk üretir; alarm/rota eşiği değildir.
POSITIVE_CORE_RADIUS_FRACTION = 0.5
METRIC_KEYS = (
    "ortalama_rgb_5x5",
    "max_rgb_5x5",
    "ortalama_rgb_9x9",
    "parlak_artis_orani_5x5",
    "koyulasma_orani_5x5",
    "soil_mask_orani_5x5",
    "kalici_bitki_orani_5x5",
    "ciplak_zemin_orani_5x5",
    "koyu_alt_toprak_proxy_orani_5x5",
)
# Yalnız mesafe normalizasyonu içindir; eşik veya üretim filtresi değildir.
METRIC_SCALES = {
    "ortalama_rgb_5x5": 0.05,
    "max_rgb_5x5": 0.12,
    "ortalama_rgb_9x9": 0.05,
    "parlak_artis_orani_5x5": 0.35,
    "koyulasma_orani_5x5": 0.35,
    "soil_mask_orani_5x5": 0.10,
    "kalici_bitki_orani_5x5": 0.30,
    "ciplak_zemin_orani_5x5": 0.45,
    "koyu_alt_toprak_proxy_orani_5x5": 0.20,
}


def _distance(left, right):
    parts = []
    for key in METRIC_KEYS:
        scale = METRIC_SCALES[key]
        parts.append(((float(left[key]) - float(right[key])) / scale) ** 2)
    return math.sqrt(sum(parts) / len(parts))


def _positive_core_radius(positive, negatives):
    nearest_negative_distance = min(_distance(positive, item) for item in negatives.values())
    return nearest_negative_distance * POSITIVE_CORE_RADIUS_FRACTION


def _missing_reference_ids(by_id):
    available = set(by_id)
    missing = []
    if CORE_POSITIVE_ID not in available:
        missing.append(CORE_POSITIVE_ID)
    missing.extend(sorted(CORE_NEGATIVE_IDS - available))
    return missing


def _reference_bank(region_arrays):
    by_id = {}
    for region_key, arrays in region_arrays.items():
        for item in localized._reference_checks(region_key, arrays):
            item_id = str(item.get("id") or "")
            if item_id == CORE_POSITIVE_ID or item_id in CORE_NEGATIVE_IDS:
                by_id[item_id] = item
    missing = _missing_reference_ids(by_id)
    if missing:
        raise RuntimeError(f"Eksik çekirdek regresyon referansı: {missing}")
    return by_id


def _rank_candidate(candidate, positive, negatives, positive_core_radius):
    positive_distance = _distance(candidate, positive)
    nearest_negative_id, nearest_negative_distance = min(
        ((item_id, _distance(candidate, item)) for item_id, item in negatives.items()),
        key=lambda pair: pair[1],
    )
    margin = nearest_negative_distance - positive_distance
    core_ratio = positive_distance / positive_core_radius if positive_core_radius > 0 else math.inf
    return {
        **candidate,
        "dogrulanmis_kazi_imza_uzakligi": round(positive_distance, 4),
        "en_yakin_cekirdek_yanlis_pozitif_id": nearest_negative_id,
        "en_yakin_cekirdek_yanlis_pozitif_imza_uzakligi": round(nearest_negative_distance, 4),
        "pozitif_ayrim_marji": round(margin, 4),
        "dogrulanmis_kaziya_daha_yakin": bool(margin > 0),
        "pozitif_cekirdek_yaricap": round(positive_core_radius, 4),
        "pozitif_cekirdek_orani": round(core_ratio, 4) if math.isfinite(core_ratio) else None,
        "pozitif_cekirdek_icinde": bool(positive_distance <= positive_core_radius),
        "ana_esik": bool(candidate.get("spektral_etki_alani_m2", 0) >= MAIN_THRESHOLD_M2),
        "mikro_bant": bool(
            MICRO_RANGE_M2[0]
            <= candidate.get("spektral_etki_alani_m2", 0)
            <= MICRO_RANGE_M2[1]
        ),
        "alarm": False,
        "saha_gorevi": False,
    }


def _self_check():
    positive = {
        "ortalama_rgb_5x5": 0.0443,
        "max_rgb_5x5": 0.1124,
        "ortalama_rgb_9x9": 0.0437,
        "parlak_artis_orani_5x5": 0.28,
        "koyulasma_orani_5x5": 0.24,
        "soil_mask_orani_5x5": 0.0,
        "kalici_bitki_orani_5x5": 0.0,
        "ciplak_zemin_orani_5x5": 0.92,
        "koyu_alt_toprak_proxy_orani_5x5": 0.04,
    }
    reisdere = {
        "ortalama_rgb_5x5": 0.1144,
        "max_rgb_5x5": 0.4131,
        "ortalama_rgb_9x9": 0.0732,
        "parlak_artis_orani_5x5": 0.20,
        "koyulasma_orani_5x5": 0.64,
        "soil_mask_orani_5x5": 0.0,
        "kalici_bitki_orani_5x5": 0.08,
        "ciplak_zemin_orani_5x5": 0.72,
        "koyu_alt_toprak_proxy_orani_5x5": 0.36,
    }
    near_positive = dict(positive)
    near_positive["max_rgb_5x5"] = 0.125
    assert _distance(positive, positive) == 0.0
    assert _distance(near_positive, positive) < _distance(near_positive, reisdere)

    core_radius = _positive_core_radius(positive, {"reisdere": reisdere})
    assert math.isclose(core_radius, _distance(positive, reisdere) / 2.0)
    ranked_positive = _rank_candidate(positive, positive, {"reisdere": reisdere}, core_radius)
    assert ranked_positive["pozitif_cekirdek_icinde"] is True
    assert ranked_positive["pozitif_cekirdek_orani"] == 0.0
    ranked_negative = _rank_candidate(reisdere, positive, {"reisdere": reisdere}, core_radius)
    assert ranked_negative["pozitif_cekirdek_icinde"] is False

    complete_reference_ids = {CORE_POSITIVE_ID, *CORE_NEGATIVE_IDS}
    assert _missing_reference_ids(complete_reference_ids) == []
    assert _missing_reference_ids(CORE_NEGATIVE_IDS) == [CORE_POSITIVE_ID]


def audit():
    _self_check()
    region_arrays = {}
    errors = {}
    for region_key in ("cesme", "uzunkuyu"):
        try:
            region_arrays[region_key] = signature._region_arrays(region_key)
        except Exception as exc:
            errors[region_key] = f"{type(exc).__name__}: {exc}"

    if not region_arrays:
        raise RuntimeError(f"Hiçbir bölge için Sentinel dizisi üretilemedi: {errors}")

    references = _reference_bank(region_arrays)
    positive = references[CORE_POSITIVE_ID]
    negatives = {item_id: references[item_id] for item_id in CORE_NEGATIVE_IDS}
    positive_core_radius = _positive_core_radius(positive, negatives)
    nearest_reference_negative_id, nearest_reference_negative_distance = min(
        ((item_id, _distance(positive, item)) for item_id, item in negatives.items()),
        key=lambda pair: pair[1],
    )

    regions = {}
    all_ranked = []
    for region_key, arrays in region_arrays.items():
        discovered = localized._discover(region_key, arrays)
        low_contrast = [
            item
            for item in discovered
            if item.get("seed_sinifi") == "DUSUK_KONTRAST_KAZI_PROXY"
        ]
        ranked = [
            _rank_candidate(item, positive, negatives, positive_core_radius)
            for item in low_contrast
        ]
        ranked.sort(
            key=lambda item: (
                not item["pozitif_cekirdek_icinde"],
                -item["pozitif_ayrim_marji"],
                item["dogrulanmis_kazi_imza_uzakligi"],
                -item["max_rgb_5x5"],
            )
        )
        all_ranked.extend({"bolge_anahtari": region_key, **item} for item in ranked)
        regions[region_key] = {
            "bolge": satellite.REGIONS[region_key]["label"],
            "onceki_tarih": arrays["older_date"],
            "son_tarih": arrays["latest_date"],
            "dusuk_kontrast_aday_sayisi": len(ranked),
            "pozitife_daha_yakin_aday_sayisi": sum(
                1 for item in ranked if item["dogrulanmis_kaziya_daha_yakin"]
            ),
            "pozitif_cekirdek_aday_sayisi": sum(
                1 for item in ranked if item["pozitif_cekirdek_icinde"]
            ),
            "referans_benzerlik_kisa_listesi": ranked[:12],
        }

    all_ranked.sort(
        key=lambda item: (
            not item["pozitif_cekirdek_icinde"],
            -item["pozitif_ayrim_marji"],
            item["dogrulanmis_kazi_imza_uzakligi"],
        )
    )
    return {
        "surum": 2,
        "amac": "Düşük-kontrast kazı seedlerini 1 gerçek kazı + 4 çekirdek tarla/bahçe yanlış pozitifiyle referans-benzerlik sırasına koymak",
        "gercek_derinlik_olcumu": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "cekirdek_regresyon_pozitif_id": CORE_POSITIVE_ID,
        "cekirdek_regresyon_yanlis_pozitif_idleri": sorted(CORE_NEGATIVE_IDS),
        "pozitif_cekirdek_yaricap_orani": POSITIVE_CORE_RADIUS_FRACTION,
        "pozitif_cekirdek_yaricap": round(positive_core_radius, 4),
        "pozitife_en_yakin_cekirdek_yanlis_pozitif_id": nearest_reference_negative_id,
        "pozitife_en_yakin_cekirdek_yanlis_pozitif_imza_uzakligi": round(
            nearest_reference_negative_distance, 4
        ),
        "eksik_bolge_hatalari": errors,
        "toplam_dusuk_kontrast_aday": len(all_ranked),
        "toplam_pozitife_daha_yakin_aday": sum(
            1 for item in all_ranked if item["dogrulanmis_kaziya_daha_yakin"]
        ),
        "toplam_pozitif_cekirdek_aday": sum(
            1 for item in all_ranked if item["pozitif_cekirdek_icinde"]
        ),
        "genel_referans_benzerlik_kisa_listesi": all_ranked[:20],
        "bolgeler": regions,
        "not": (
            "Bu çıktı yalnız diagnostiktir. İmza mesafesi gerçek kazı derinliği veya saha doğrulaması değildir; "
            "tek pozitif referans nedeniyle rota/alarm üretmez. Pozitif çekirdek yarıçapı, doğrulanmış kazı ile "
            "en yakın dört çekirdek tarla-bahçe yanlış pozitifinden en yakınının imza mesafesinin yarısıdır; "
            "bu yalnız konservatif bir benzerlik komşuluğudur. 250 m² ana eşik ve 150–249 m² MİKRO politikası değişmez."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("localized excavation reference rank self-check: ok")
        return
    payload = audit()
    OUTPUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
