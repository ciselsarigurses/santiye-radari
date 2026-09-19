"""Seed-merkezli morfoloji bileşeninin gerçekten seed merkezine oturup oturmadığını ölçer.

Bu katman yalnız diagnostiktir. Alarm, saha görevi, rota, 250 m² ana eşik veya
150–249 m² MİKRO politikasını değiştirmez. Amaç, mevcut morfoloji puanındaki tavan
doygunluğunun bir kısmının seed merkezinden uzaktaki yakın komşu bileşenlerin yanlışlıkla
kompakt kazı geometrisi gibi puanlanmasından kaynaklanıp kaynaklanmadığını ölçmektir.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

import satellite
import seed_centered_excavation_morphology_audit as morphology


OUTPUT_JSON = Path(__file__).with_name("seed_component_alignment_review.json")
CAP_SCORE = 100
ALIGNED_DISTANCE_PX = 1.5
CORE_POSITIVE_ID = "FN-20260916-CESME-001"
CORE_FALSE_POSITIVE_IDS = {
    "FP-20260916-REISDERE-001",
    "FP-20260917-UZUNKUYU-001",
    "FP-20260917-MUSALLA-001",
    "FP-20260917-MUSALLA-002",
}


def _nearest_component_distance(mask: np.ndarray, center_row: int, center_col: int):
    components = satellite._connected_components(mask)
    if not components:
        return None, False, 0

    best_distance = None
    best_size = 0
    center_inside = False
    for component in components:
        if (center_row, center_col) in component:
            center_inside = True
        local_best = min(
            math.hypot(r - center_row, c - center_col)
            for r, c in component
        )
        if best_distance is None or local_best < best_distance:
            best_distance = local_best
            best_size = len(component)

    return float(best_distance), center_inside, int(best_size)


def _alignment_at(arrays, latitude, longitude):
    row, col = morphology.signature._row_col(
        latitude, longitude, arrays["bbox"], arrays["shape"]
    )
    valid, cr, cc = morphology._local_slice(arrays["valid"], row, col)
    rgb, _, _ = morphology._local_slice(arrays["rgb_difference"], row, col)
    brightness, _, _ = morphology._local_slice(arrays["brightness_gain"], row, col)
    latest_ndvi, _, _ = morphology._local_slice(arrays["latest_ndvi"], row, col)
    vegetation_loss, _, _ = morphology._local_slice(arrays["vegetation_loss"], row, col)

    disturbance = (
        valid
        & (rgb >= morphology.DISTURBANCE_RGB)
        & (latest_ndvi < morphology.DISTURBANCE_NDVI_MAX)
        & (
            (brightness > 0.035)
            | (brightness < -0.025)
            | (vegetation_loss > 0.14)
        )
    )
    distance, center_inside, component_size = _nearest_component_distance(
        disturbance, cr, cc
    )
    return {
        "en_yakin_bilesen_merkez_mesafe_piksel": (
            round(distance, 3) if distance is not None else None
        ),
        "seed_merkezi_bilesen_icinde": bool(center_inside),
        "en_yakin_bilesen_piksel": component_size,
        "seed_bilesen_hizali": bool(
            distance is not None and distance <= ALIGNED_DISTANCE_PX
        ),
    }


def _analyze_region(region_key: str) -> dict:
    arrays = morphology.signature._region_arrays(region_key)
    seeds = morphology.seed_audit._discover(region_key, arrays)
    scored = []
    for candidate in seeds:
        score = morphology._score_at(arrays, candidate["enlem"], candidate["boylam"])
        alignment = _alignment_at(arrays, candidate["enlem"], candidate["boylam"])
        scored.append({**candidate, **score, **alignment})

    scored.sort(
        key=lambda x: (
            -int(x["seed_merkezli_morfoloji_puani"]),
            -float(x.get("max_rgb_5x5") or 0),
        )
    )
    priority_rows = morphology._reference_priority_rows(region_key)
    exported = morphology._priority_export_candidates(scored, priority_rows)
    capped = [
        item for item in exported
        if int(item.get("seed_merkezli_morfoloji_puani") or 0) >= CAP_SCORE
    ]

    feedback_rows = []
    bbox = arrays["bbox"]
    for feedback in morphology._feedback_records():
        if not morphology.signature._point_in_bbox(feedback, bbox):
            continue
        nearest, distance_m = morphology._nearest_candidate(feedback, scored)
        matched = bool(
            nearest is not None
            and distance_m is not None
            and distance_m <= morphology.MATCH_RADIUS_M
        )
        feedback_rows.append(
            {
                "id": feedback.get("id"),
                "sonuc": str(feedback.get("sonuc") or "").upper(),
                "en_yakin_seed_mesafe_m": round(distance_m, 1) if distance_m is not None else None,
                "seed_yakaladi": matched,
                "morfoloji_puani": (
                    nearest.get("seed_merkezli_morfoloji_puani") if matched else None
                ),
                "bilesen_merkez_mesafe_piksel": (
                    nearest.get("en_yakin_bilesen_merkez_mesafe_piksel") if matched else None
                ),
                "seed_merkezi_bilesen_icinde": (
                    nearest.get("seed_merkezi_bilesen_icinde") if matched else None
                ),
                "seed_bilesen_hizali": (
                    nearest.get("seed_bilesen_hizali") if matched else None
                ),
            }
        )

    return {
        "bolge": satellite.REGIONS[region_key]["label"],
        "durum": "ok",
        "onceki_tarih": arrays["older_date"],
        "son_tarih": arrays["latest_date"],
        "lokal_seed_sayisi": len(scored),
        "disari_aktarilan_aday_sayisi": len(exported),
        "tavan_100_puanli_sayi": len(capped),
        "tavan_100_hizali_sayi": sum(
            1 for item in capped if item.get("seed_bilesen_hizali") is True
        ),
        "tavan_100_hizasiz_sayi": sum(
            1 for item in capped if item.get("seed_bilesen_hizali") is False
        ),
        "tavan_100_seed_merkezi_bilesende_sayi": sum(
            1 for item in capped if item.get("seed_merkezi_bilesen_icinde") is True
        ),
        "tavan_100_hizali_oran": round(
            sum(1 for item in capped if item.get("seed_bilesen_hizali") is True)
            / max(len(capped), 1),
            4,
        ),
        "tavan_100_hizasiz_oran": round(
            sum(1 for item in capped if item.get("seed_bilesen_hizali") is False)
            / max(len(capped), 1),
            4,
        ),
        "saha_referans_hizalama": feedback_rows,
        "ornek_tavan_adaylar": [
            {
                "enlem": item.get("enlem"),
                "boylam": item.get("boylam"),
                "morfoloji_puani": item.get("seed_merkezli_morfoloji_puani"),
                "bilesen_merkez_mesafe_piksel": item.get(
                    "en_yakin_bilesen_merkez_mesafe_piksel"
                ),
                "seed_merkezi_bilesen_icinde": item.get("seed_merkezi_bilesen_icinde"),
                "seed_bilesen_hizali": item.get("seed_bilesen_hizali"),
            }
            for item in capped[:20]
        ],
    }


def _core_summary(regions: dict) -> dict:
    rows = {}
    for region in regions.values():
        for item in region.get("saha_referans_hizalama") or []:
            item_id = str(item.get("id") or "")
            if item_id == CORE_POSITIVE_ID or item_id in CORE_FALSE_POSITIVE_IDS:
                rows[item_id] = item
    positive = rows.get(CORE_POSITIVE_ID)
    false_rows = [rows[x] for x in sorted(CORE_FALSE_POSITIVE_IDS) if x in rows]
    return {
        "gercek_kazi_bulundu": positive is not None,
        "gercek_kazi_seed_bilesen_hizali": (
            positive.get("seed_bilesen_hizali") if positive else None
        ),
        "gercek_kazi_bilesen_merkez_mesafe_piksel": (
            positive.get("bilesen_merkez_mesafe_piksel") if positive else None
        ),
        "cekirdek_yanlis_pozitif_bulunan_sayi": len(false_rows),
        "cekirdek_yanlis_pozitif_hizali_sayi": sum(
            1 for item in false_rows if item.get("seed_bilesen_hizali") is True
        ),
        "cekirdek_yanlis_pozitif_hizasiz_sayi": sum(
            1 for item in false_rows if item.get("seed_bilesen_hizali") is False
        ),
    }


def _self_check() -> None:
    mask = np.zeros((9, 9), dtype=bool)
    mask[4, 4] = True
    mask[4, 5] = True
    distance, inside, size = _nearest_component_distance(mask, 4, 4)
    assert distance == 0.0
    assert inside is True
    assert size == 2

    mask = np.zeros((9, 9), dtype=bool)
    mask[1, 1] = True
    distance, inside, size = _nearest_component_distance(mask, 4, 4)
    assert round(distance, 3) == round(math.sqrt(18), 3)
    assert inside is False
    assert size == 1

    mask = np.zeros((9, 9), dtype=bool)
    distance, inside, size = _nearest_component_distance(mask, 4, 4)
    assert distance is None
    assert inside is False
    assert size == 0


def audit() -> dict:
    regions = {}
    errors = []
    for region_key in ("cesme", "uzunkuyu"):
        try:
            regions[region_key] = _analyze_region(region_key)
        except Exception as exc:
            errors.append({"bolge": region_key, "hata": f"{type(exc).__name__}: {exc}"})
            regions[region_key] = {
                "bolge": satellite.REGIONS[region_key]["label"],
                "durum": "hata",
                "hata": f"{type(exc).__name__}: {exc}",
            }

    return {
        "surum": 1,
        "amac": "Morfoloji tavan doygunluğunda seed-bileşen merkez hizalamasını ölçmek",
        "uretim_filtresi": False,
        "alarm": False,
        "saha_gorevi": False,
        "rota_degistirir": False,
        "ana_uretim_esigi_degisti": False,
        "mikro_politikasi_degisti": False,
        "hizali_mesafe_esigi_piksel": ALIGNED_DISTANCE_PX,
        "hatalar": errors,
        "bolgeler": regions,
        "cekirdek_regresyon_hizalama_ozeti": _core_summary(regions),
        "not": (
            "Bu çıktı yalnız morfoloji skorunun neden doygunlaştığını ölçer. Seed merkezinden uzak "
            "bir komşu bileşen, bağımsız kanıt olmadan temel/kepçe lehine yeni operasyonel sinyal sayılmaz."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("seed component alignment self-check: ok")
        return
    payload = audit()
    OUTPUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
