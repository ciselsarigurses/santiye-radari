"""Lokal kazı seed'lerinin çevresinde temel/kepçe kazısı morfolojisini ölçer.

Bu katman yalnız diagnostiktir. Alarm, saha görevi veya rota üretmez; 250 m² ana
üretim eşiği ile 150–249 m² MİKRO politikası değişmez. Sentinel-2 10 m veriden
gerçek kazı derinliği ölçüldüğü iddia edilmez.

Amaç, geniş değişim bileşenlerini şekil olarak puanlamak yerine, saha kalibrasyonunda
gerçek kazıyı yakalayan lokal/seyrek seed'in hemen çevresini incelemektir. Böylece
"tarla düzeldi" ile "lokal temel/kepçe müdahalesi" ayrımı için ikinci, test edilebilir
bir morfoloji hipotezi üretilir.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

import excavation_reference_signature_audit as signature
import localized_excavation_seed_audit as seed_audit
import satellite


OUTPUT_JSON = Path(__file__).with_name("seed_centered_excavation_morphology_review.json")
REFERENCE_RANK_JSON = Path(__file__).with_name("localized_excavation_reference_rank_review.json")
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
MATCH_RADIUS_M = 45.0
REFERENCE_PRIORITY_MATCH_RADIUS_M = 5.0
MORPHOLOGY_EXPORT_LIMIT = 120
HIGH_SCORE = 65
MEDIUM_SCORE = 45

DISTURBANCE_RGB = 0.10
DISTURBANCE_NDVI_MAX = 0.40
WINDOW_RADIUS = 4


def _distance_m(a, b):
    lat1, lon1 = float(a[0]), float(a[1])
    lat2, lon2 = float(b[0]), float(b[1])
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _local_slice(array, row, col, radius=WINDOW_RADIUS):
    r0 = max(row - radius, 0)
    r1 = min(row + radius + 1, array.shape[0])
    c0 = max(col - radius, 0)
    c1 = min(col + radius + 1, array.shape[1])
    return array[r0:r1, c0:c1], row - r0, col - c0


def _shape_features(component):
    if not component:
        return {
            "bilesen_piksel": 0,
            "uzun_kisa_orani": None,
            "kutu_doluluk_orani": 0.0,
        }
    pixels = np.asarray(component, dtype="int32")
    rows = pixels[:, 0]
    cols = pixels[:, 1]
    row_span = int(rows.max() - rows.min() + 1)
    col_span = int(cols.max() - cols.min() + 1)
    short = max(min(row_span, col_span), 1)
    long = max(row_span, col_span)
    aspect = long / short
    fill = len(component) / max(row_span * col_span, 1)
    return {
        "bilesen_piksel": int(len(component)),
        "uzun_kisa_orani": round(float(aspect), 3),
        "kutu_doluluk_orani": round(float(fill), 3),
    }


def _center_component(mask, center_row, center_col):
    components = satellite._connected_components(mask)
    if not components:
        return []
    for component in components:
        if (center_row, center_col) in component:
            return component
    best = None
    best_distance = None
    for component in components:
        for r, c in component:
            distance = (r - center_row) ** 2 + (c - center_col) ** 2
            if best_distance is None or distance < best_distance:
                best = component
                best_distance = distance
    return best or []


def _score_at(arrays, latitude, longitude):
    row, col = signature._row_col(latitude, longitude, arrays["bbox"], arrays["shape"])

    valid, cr, cc = _local_slice(arrays["valid"], row, col)
    rgb, _, _ = _local_slice(arrays["rgb_difference"], row, col)
    brightness, _, _ = _local_slice(arrays["brightness_gain"], row, col)
    latest_ndvi, _, _ = _local_slice(arrays["latest_ndvi"], row, col)
    vegetation_loss, _, _ = _local_slice(arrays["vegetation_loss"], row, col)
    soil, _, _ = _local_slice(arrays["current_soil"], row, col)

    yy, xx = np.ogrid[:valid.shape[0], :valid.shape[1]]
    distance = np.sqrt((yy - cr) ** 2 + (xx - cc) ** 2)
    core = valid & (distance <= 2.0)
    outer = valid & (distance > 2.0) & (distance <= 4.5)
    core_count = max(int(np.count_nonzero(core)), 1)
    outer_count = max(int(np.count_nonzero(outer)), 1)

    disturbance = (
        valid
        & (rgb >= DISTURBANCE_RGB)
        & (latest_ndvi < DISTURBANCE_NDVI_MAX)
        & (
            (brightness > 0.035)
            | (brightness < -0.025)
            | (vegetation_loss > 0.14)
        )
    )
    component = _center_component(disturbance, cr, cc)
    shape = _shape_features(component)

    core_rgb = float(np.mean(rgb[core])) if np.any(core) else 0.0
    outer_rgb = float(np.mean(rgb[outer])) if np.any(outer) else 0.0
    core_peak = float(np.max(rgb[core])) if np.any(core) else 0.0
    concentration = core_rgb / max(outer_rgb, 0.01)
    peak_ratio = core_peak / max(core_rgb, 0.01)

    core_disturbance = float(np.count_nonzero(disturbance & core) / core_count)
    outer_disturbance = float(np.count_nonzero(disturbance & outer) / outer_count)
    bright_fraction = float(np.count_nonzero((brightness > 0.035) & core) / core_count)
    dark_fraction = float(np.count_nonzero((brightness < -0.025) & core) / core_count)
    soil_fraction = float(np.count_nonzero(soil & core) / core_count)

    score = 0
    positives = []
    negatives = []

    component_pixels = int(shape["bilesen_piksel"])
    aspect = shape["uzun_kisa_orani"]
    fill = float(shape["kutu_doluluk_orani"])

    if 1 <= component_pixels <= 8:
        score += 20
        positives.append("lokal ve küçük müdahale bileşeni")
    elif component_pixels <= 14:
        score += 8
    elif component_pixels > 20:
        score -= 20
        negatives.append("geniş yerel bozulma bileşeni")

    if aspect is not None and aspect <= 3.0:
        score += 10
        positives.append("uzun-şerit olmayan yerel geometri")
    elif aspect is not None and aspect >= 5.0:
        score -= 15
        negatives.append("şerit/tarla benzeri geometri")

    if fill >= 0.40:
        score += 10
        positives.append("kapalı/kompakt yerel doluluk")

    if concentration >= 1.35:
        score += 15
        positives.append("merkez çevreden daha güçlü değişiyor")
    elif concentration < 1.05:
        score -= 15
        negatives.append("merkez-çevre kontrastı zayıf")

    if peak_ratio >= 2.5:
        score += 10
        positives.append("lokal kuvvetli çekirdek")

    if 0.04 <= core_disturbance <= 0.45:
        score += 10
        positives.append("çekirdek değişimi seyrek/lokal")
    elif core_disturbance >= 0.65:
        score -= 20
        negatives.append("çekirdek değişimi fazla homojen")

    if outer_disturbance <= 0.18:
        score += 15
        positives.append("dış çevre büyük ölçüde stabil")
    elif outer_disturbance >= 0.40:
        score -= 25
        negatives.append("dış çevrede geniş yüzey hareketi")

    if 0.08 <= bright_fraction <= 0.55:
        score += 10
        positives.append("sınırlı parlak hafriyat/çıplak-zemin zonu")
    elif bright_fraction >= 0.75:
        score -= 20
        negatives.append("geniş parlak yüzey/tarla etkisi")

    if 0.01 <= soil_fraction <= 0.12:
        score += 10
        positives.append("toprak sinyali lokal ölçekte sınırlı")
    elif soil_fraction >= 0.25:
        score -= 30
        negatives.append("geniş toprak/tarla sinyali")

    if 0.02 <= dark_fraction <= 0.35 and bright_fraction >= 0.08:
        score += 5
        positives.append("parlak-koyu ikincil zon birlikteliği")
    elif dark_fraction >= 0.60:
        score -= 20
        negatives.append("geniş koyulaşma etkisi")

    score = max(0, min(100, int(round(score))))
    if score >= HIGH_SCORE:
        level = "YUKSEK"
    elif score >= MEDIUM_SCORE:
        level = "ORTA"
    else:
        level = "DUSUK"

    return {
        "enlem": round(float(latitude), 6),
        "boylam": round(float(longitude), 6),
        **shape,
        "merkez_rgb_ortalama": round(core_rgb, 4),
        "dis_cevre_rgb_ortalama": round(outer_rgb, 4),
        "merkez_dis_konsantrasyon": round(concentration, 3),
        "tepe_merkez_orani": round(peak_ratio, 3),
        "cekirdek_degisim_orani": round(core_disturbance, 3),
        "dis_cevre_degisim_orani": round(outer_disturbance, 3),
        "parlak_zon_orani": round(bright_fraction, 3),
        "koyu_zon_orani": round(dark_fraction, 3),
        "soil_orani": round(soil_fraction, 3),
        "seed_merkezli_morfoloji_puani": score,
        "seed_merkezli_morfoloji_seviyesi": level,
        "pozitif_kanitlar": positives,
        "negatif_kanitlar": negatives,
        "alarm": False,
        "saha_gorevi": False,
        "uyari": "Gerçek kazı derinliği değildir; Sentinel-2 10 m lokal morfoloji proxy'sidir.",
    }


def _feedback_records():
    return [
        item for item in signature._load_feedback()
        if str(item.get("sonuc") or "").upper() in {"YANLIS_POZITIF", "DOGRULANMIS_KAZI"}
    ]


def _nearest_candidate(item, candidates):
    target = (float(item["enlem"]), float(item["boylam"]))
    nearest = None
    nearest_distance = None
    for candidate in candidates:
        distance = _distance_m(target, (candidate["enlem"], candidate["boylam"]))
        if nearest_distance is None or distance < nearest_distance:
            nearest = candidate
            nearest_distance = distance
    return nearest, nearest_distance


def _reference_priority_rows(region_key):
    try:
        payload = json.loads(REFERENCE_RANK_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [
        item
        for item in (payload.get("yeni_alan_esikli_cekirdek_kisa_listesi") or [])
        if isinstance(item, dict)
        and item.get("bolge_anahtari") == region_key
        and "enlem" in item
        and "boylam" in item
    ]


def _priority_export_candidates(scored, priority_rows, base_limit=MORPHOLOGY_EXPORT_LIMIT):
    selected = [dict(item) for item in scored[:base_limit]]
    for item in selected:
        item["referans_cekirdek_oncelikli"] = any(
            _distance_m(
                (item["enlem"], item["boylam"]),
                (priority["enlem"], priority["boylam"]),
            ) <= REFERENCE_PRIORITY_MATCH_RADIUS_M
            for priority in priority_rows
        )

    for priority in priority_rows:
        nearest, distance = _nearest_candidate(priority, scored)
        if nearest is None or distance is None or distance > REFERENCE_PRIORITY_MATCH_RADIUS_M:
            continue
        already_selected = any(
            _distance_m(
                (nearest["enlem"], nearest["boylam"]),
                (item["enlem"], item["boylam"]),
            ) <= REFERENCE_PRIORITY_MATCH_RADIUS_M
            for item in selected
        )
        if already_selected:
            continue
        selected.append({**nearest, "referans_cekirdek_oncelikli": True})
    return selected


def _analyze_region(region_key):
    arrays = signature._region_arrays(region_key)
    seed_candidates = seed_audit._discover(region_key, arrays)
    scored = [
        {**candidate, **_score_at(arrays, candidate["enlem"], candidate["boylam"])}
        for candidate in seed_candidates
    ]
    scored.sort(
        key=lambda x: (
            -int(x["seed_merkezli_morfoloji_puani"]),
            -float(x.get("max_rgb_5x5") or 0),
        )
    )
    priority_rows = _reference_priority_rows(region_key)
    exported_candidates = _priority_export_candidates(scored, priority_rows)

    feedback = []
    bbox = arrays["bbox"]
    for item in _feedback_records():
        if not signature._point_in_bbox(item, bbox):
            continue
        nearest, distance = _nearest_candidate(item, scored)
        matched = bool(nearest is not None and distance is not None and distance <= MATCH_RADIUS_M)
        expected = str(item.get("sonuc") or "").upper()
        detected = bool(
            matched
            and str(nearest.get("seed_merkezli_morfoloji_seviyesi") or "").upper()
            in {"ORTA", "YUKSEK"}
        )
        expected_positive = expected == "DOGRULANMIS_KAZI"
        feedback.append(
            {
                "id": item.get("id"),
                "sonuc": expected,
                "en_yakin_seed_mesafe_m": round(distance, 1) if distance is not None else None,
                "seed_yakaladi": matched,
                "seed_merkezli_morfoloji_puani": (
                    nearest.get("seed_merkezli_morfoloji_puani") if matched else None
                ),
                "seed_merkezli_morfoloji_seviyesi": (
                    nearest.get("seed_merkezli_morfoloji_seviyesi") if matched else None
                ),
                "tespit": detected,
                "regresyon_uyumlu": detected == expected_positive,
            }
        )

    failures = [item for item in feedback if not item["regresyon_uyumlu"]]
    return {
        "bolge": satellite.REGIONS[region_key]["label"],
        "durum": "ok",
        "onceki_tarih": arrays["older_date"],
        "son_tarih": arrays["latest_date"],
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "alarm": False,
        "saha_gorevi": False,
        "lokal_seed_sayisi": len(seed_candidates),
        "orta_yuksek_seed_morfoloji": sum(
            1 for x in scored
            if x["seed_merkezli_morfoloji_seviyesi"] in {"ORTA", "YUKSEK"}
        ),
        "morfoloji_disari_aktarma_tavani": MORPHOLOGY_EXPORT_LIMIT,
        "referans_cekirdek_oncelikli_girdi_sayisi": len(priority_rows),
        "referans_cekirdek_oncelikli_disari_aktarilan_sayi": sum(
            1 for item in exported_candidates if item.get("referans_cekirdek_oncelikli") is True
        ),
        "adaylar": exported_candidates,
        "saha_referans_regresyonu": feedback,
        "regresyon_uyumsuz_sayisi": len(failures),
        "regresyon_uyumsuzluklari": failures,
    }


def _self_check():
    compact = _shape_features([(2, 2), (2, 3), (3, 2), (3, 3)])
    strip = _shape_features([(2, c) for c in range(1, 7)])
    assert compact["kutu_doluluk_orani"] == 1.0
    assert compact["uzun_kisa_orani"] == 1.0
    assert strip["uzun_kisa_orani"] >= 5.0

    mask = np.zeros((9, 9), dtype=bool)
    mask[4, 4] = True
    mask[4, 5] = True
    component = _center_component(mask, 4, 4)
    assert len(component) == 2

    scored = [
        {
            "enlem": 38.30 + index * 0.001,
            "boylam": 26.30,
            "seed_merkezli_morfoloji_puani": 100 - index,
        }
        for index in range(25)
    ]
    priority = [{"enlem": scored[-1]["enlem"], "boylam": scored[-1]["boylam"]}]
    exported = _priority_export_candidates(scored, priority)
    assert len(exported) == 25
    assert exported[-1]["enlem"] == scored[-1]["enlem"]
    assert exported[-1]["referans_cekirdek_oncelikli"] is True


def audit():
    _self_check()
    regions = {}
    all_failures = []
    for region_key in ("cesme", "uzunkuyu"):
        try:
            regions[region_key] = _analyze_region(region_key)
            all_failures.extend(
                {"bolge_anahtari": region_key, **item}
                for item in regions[region_key]["regresyon_uyumsuzluklari"]
            )
        except Exception as exc:
            regions[region_key] = {
                "bolge": satellite.REGIONS[region_key]["label"],
                "durum": "hata",
                "hata": f"{type(exc).__name__}: {exc}",
            }
    return {
        "surum": 2,
        "amac": "Lokal kazı seed çevresinde temel/kepçe kazısı morfolojisini diagnostik olarak puanlamak",
        "gercek_derinlik_olcumu": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "proxy_esikleri": {"yuksek": HIGH_SCORE, "orta": MEDIUM_SCORE},
        "toplam_regresyon_uyumsuz": len(all_failures),
        "regresyon_uyumsuzluklari": all_failures,
        "bolgeler": regions,
        "not": (
            "Geniş ana morfoloji bileşenleri yerine lokal-seed merkezli ikinci hipotezdir. "
            "İlk 120 morfoloji adayı yanında, doğrulanmış kazı imzasının çekirdeğinde kalan 150 m²+ "
            "diagnostik adaylar da downstream SAR/rota denetiminde kaybolmamaları için korunur. "
            "Yeni saha pozitifleri, temporal devamlılık ve Sentinel-1/SAR desteği olmadan rotaya bağlanmaz."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("seed centered excavation morphology self-check: ok")
        return
    payload = audit()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
