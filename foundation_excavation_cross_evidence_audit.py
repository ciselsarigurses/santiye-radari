"""Temel/kepçe kazısı için iki farklı Sentinel-2 diagnostik katmanını çaprazlar.

Amaç, tek başına kompakt şekli temel kazısı sanmayı önlemek ve tarla/bahçe
yanlış pozitiflerini operasyon rotasına taşımadan önce ikinci bir lokal/seyrek
değişim kanıtı istemektir.

Bu dosya yalnız diagnostik üretir. Alarm, saha görevi veya rota kararı vermez;
250 m² ana eşik ve 150–249 m² MİKRO politikası değişmez. Sentinel-2 10 m
veriden gerçek kazı derinliği ölçüldüğü iddia edilmez.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path


BASE = Path(__file__).resolve().parent
MORPHOLOGY_JSON = BASE / "foundation_excavation_morphology_review.json"
SEED_JSON = BASE / "localized_excavation_seed_review.json"
FEEDBACK_JSON = BASE / "manual_field_feedback.json"
OUTPUT_JSON = BASE / "foundation_excavation_cross_evidence_review.json"

MATCH_RADIUS_M = 45.0
DEFAULT_FEEDBACK_RADIUS_M = 30.0
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]


def _load(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _distance_m(a, b):
    lat1, lon1 = float(a[0]), float(a[1])
    lat2, lon2 = float(b[0]), float(b[1])
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _point(item):
    try:
        return float(item["enlem"]), float(item["boylam"])
    except (KeyError, TypeError, ValueError):
        return None


def _parse_date(value):
    text = str(value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _nearest(target, records):
    target_point = _point(target)
    if target_point is None:
        return None, None
    nearest = None
    nearest_distance = None
    for record in records:
        point = _point(record)
        if point is None:
            continue
        distance = _distance_m(target_point, point)
        if nearest_distance is None or distance < nearest_distance:
            nearest = record
            nearest_distance = distance
    return nearest, nearest_distance


def _feedback_records(payload):
    if not isinstance(payload, dict):
        return []
    allowed = {"YANLIS_POZITIF", "DOGRULANMIS_KAZI", "MEVCUT_MUSTERI"}
    return [
        item
        for item in payload.get("kayitlar", [])
        if isinstance(item, dict) and str(item.get("sonuc") or "").upper() in allowed
    ]


def _feedback_guard(candidate, scene_date, feedback):
    point = _point(candidate)
    if point is None:
        return None
    matches = []
    for item in feedback:
        item_point = _point(item)
        if item_point is None:
            continue
        radius = _number(item.get("eslesme_yaricapi_m"), DEFAULT_FEEDBACK_RADIUS_M)
        distance = _distance_m(point, item_point)
        if distance > radius:
            continue
        result = str(item.get("sonuc") or "").upper()
        result_date = _parse_date(item.get("sonuc_tarihi"))
        same_or_older_scene = bool(
            scene_date is not None and result_date is not None and scene_date <= result_date
        )
        matches.append(
            {
                "id": item.get("id"),
                "sonuc": result,
                "mesafe_m": round(distance, 1),
                "sonuc_tarihi": item.get("sonuc_tarihi"),
                "ayni_veya_eski_sahne": same_or_older_scene,
            }
        )
    if not matches:
        return None
    matches.sort(key=lambda x: x["mesafe_m"])
    return matches[0]


def _cross_region(region_key, morphology_region, seed_region, feedback):
    if not isinstance(morphology_region, dict) or morphology_region.get("durum") != "ok":
        return {"durum": "atlandi", "neden": "morfoloji_hazir_degil"}
    if not isinstance(seed_region, dict) or seed_region.get("durum") != "ok":
        return {"durum": "atlandi", "neden": "lokal_seed_hazir_degil"}

    morphology_date = _parse_date(morphology_region.get("son_tarih"))
    seed_date = _parse_date(seed_region.get("son_tarih"))
    if morphology_date is None or seed_date is None or morphology_date != seed_date:
        return {"durum": "atlandi", "neden": "sahne_tarihi_eslesmiyor"}

    seeds = [
        item
        for item in seed_region.get("lokal_seed_diagnostik_adaylari", [])
        if isinstance(item, dict) and _point(item) is not None
    ]
    morphology = [
        item
        for item in morphology_region.get("en_yuksek_ornekler", [])
        if isinstance(item, dict)
        and _point(item) is not None
        and str(item.get("temel_kazi_proxy_seviyesi") or "").upper() in {"ORTA", "YUKSEK"}
    ]

    combined = []
    for candidate in morphology:
        seed, seed_distance = _nearest(candidate, seeds)
        if seed is None or seed_distance is None or seed_distance > MATCH_RADIUS_M:
            continue
        guard = _feedback_guard(candidate, morphology_date, feedback)
        blocked = bool(
            guard
            and guard.get("ayni_veya_eski_sahne")
            and guard.get("sonuc") in {"YANLIS_POZITIF", "MEVCUT_MUSTERI"}
        )
        combined.append(
            {
                "enlem": round(_number(candidate.get("enlem")), 6),
                "boylam": round(_number(candidate.get("boylam")), 6),
                "alan_m2": round(_number(candidate.get("alan_m2"))),
                "morfoloji_puani": candidate.get("temel_kazi_proxy_puani"),
                "morfoloji_seviyesi": candidate.get("temel_kazi_proxy_seviyesi"),
                "lokal_seed_mesafe_m": round(seed_distance, 1),
                "lokal_seed_rgb_5x5": seed.get("ortalama_rgb_5x5"),
                "lokal_seed_max_rgb_5x5": seed.get("max_rgb_5x5"),
                "lokal_seed_rgb_9x9": seed.get("ortalama_rgb_9x9"),
                "lokal_seed_soil_orani_5x5": seed.get("soil_mask_orani_5x5"),
                "saha_kalibrasyon_koruma": guard,
                "kalibrasyonla_engellendi": blocked,
                "coklu_kanit_diagnostik": not blocked,
                "alarm": False,
                "saha_gorevi": False,
                "not": (
                    "Morfoloji + lokal/seyrek değişim aynı noktada kesişiyor; yine de tek Sentinel-2 "
                    "sahne çifti üzerinden türetilen diagnostiktir ve bağımsız SAR/temporal/saha kanıtı "
                    "olmadan operasyon rotasına yükseltilmez."
                ),
            }
        )

    combined.sort(
        key=lambda item: (
            bool(item.get("kalibrasyonla_engellendi")),
            -_number(item.get("morfoloji_puani")),
            _number(item.get("lokal_seed_mesafe_m"), 9999),
        )
    )
    return {
        "durum": "ok",
        "son_tarih": morphology_region.get("son_tarih"),
        "morfoloji_orta_yuksek_ornek": len(morphology),
        "lokal_seed_adayi": len(seeds),
        "coklu_kanit_eslesmesi": len(combined),
        "kalibrasyonla_engellenen": sum(1 for x in combined if x.get("kalibrasyonla_engellendi")),
        "adaylar": combined,
    }


def _calibration_regression(seed_payload, feedback):
    results = []
    all_seed_records = []
    for region in (seed_payload or {}).get("bolgeler", {}).values():
        if not isinstance(region, dict):
            continue
        all_seed_records.extend(
            item
            for item in region.get("lokal_seed_diagnostik_adaylari", [])
            if isinstance(item, dict)
        )

    for item in feedback:
        expected = str(item.get("sonuc") or "").upper()
        seed, distance = _nearest(item, all_seed_records)
        radius = max(_number(item.get("eslesme_yaricapi_m"), DEFAULT_FEEDBACK_RADIUS_M), MATCH_RADIUS_M)
        seed_match = bool(seed is not None and distance is not None and distance <= radius)
        if expected == "DOGRULANMIS_KAZI":
            ok = seed_match
        elif expected == "YANLIS_POZITIF":
            ok = not seed_match
        else:
            ok = True
        results.append(
            {
                "id": item.get("id"),
                "sonuc": expected,
                "lokal_seed_yakaladi": seed_match,
                "en_yakin_seed_mesafe_m": round(distance, 1) if distance is not None else None,
                "regresyon_uyumlu": bool(ok),
            }
        )
    return results


def audit(morphology_payload=None, seed_payload=None, feedback_payload=None):
    morphology_payload = morphology_payload or _load(MORPHOLOGY_JSON)
    seed_payload = seed_payload or _load(SEED_JSON)
    feedback_payload = feedback_payload or _load(FEEDBACK_JSON)
    if not isinstance(morphology_payload, dict) or not isinstance(seed_payload, dict):
        raise RuntimeError("Morfoloji veya lokal seed diagnostik çıktısı bulunamadı.")

    feedback = _feedback_records(feedback_payload)
    regions = {}
    morphology_regions = morphology_payload.get("bolgeler") or {}
    seed_regions = seed_payload.get("bolgeler") or {}
    for region_key in sorted(set(morphology_regions) | set(seed_regions)):
        regions[region_key] = _cross_region(
            region_key,
            morphology_regions.get(region_key),
            seed_regions.get(region_key),
            feedback,
        )

    regression = _calibration_regression(seed_payload, feedback)
    failures = [item for item in regression if not item.get("regresyon_uyumlu")]
    active_cross = sum(
        1
        for region in regions.values()
        if isinstance(region, dict)
        for item in region.get("adaylar", [])
        if item.get("coklu_kanit_diagnostik")
    )
    return {
        "surum": 1,
        "amac": "Temel/kepçe kazısı için morfoloji ve lokal/seyrek değişim diagnostiklerini çaprazlamak",
        "gercek_derinlik_olcumu": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "alarm": False,
        "saha_gorevi": False,
        "eslesme_yaricapi_m": MATCH_RADIUS_M,
        "bolgeler": regions,
        "kalibrasyon_regresyonu": regression,
        "toplam_regresyon_uyumsuz": len(failures),
        "regresyon_uyumsuzluklari": failures,
        "aktif_coklu_kanit_diagnostik": active_cross,
        "rota_kapisi_hazir": False,
        "rota_kapisi_notu": (
            "Çapraz kanıt yalnız diagnostiktir. Pozitif saha referansı sayısı henüz yetersiz; "
            "ayrıca Sentinel-1/SAR veya ikinci tarih desteği olmadan operasyona bağlanmaz."
        ),
    }


def _self_check():
    morphology = {
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "son_tarih": "15.09.2026",
                "en_yuksek_ornekler": [
                    {
                        "enlem": 38.300000,
                        "boylam": 26.300000,
                        "alan_m2": 500,
                        "temel_kazi_proxy_puani": 80,
                        "temel_kazi_proxy_seviyesi": "YUKSEK",
                    },
                    {
                        "enlem": 38.310000,
                        "boylam": 26.310000,
                        "alan_m2": 600,
                        "temel_kazi_proxy_puani": 75,
                        "temel_kazi_proxy_seviyesi": "YUKSEK",
                    },
                ],
            }
        }
    }
    seeds = {
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "son_tarih": "15.09.2026",
                "lokal_seed_diagnostik_adaylari": [
                    {
                        "enlem": 38.300010,
                        "boylam": 26.300010,
                        "ortalama_rgb_5x5": 0.06,
                        "max_rgb_5x5": 0.28,
                        "ortalama_rgb_9x9": 0.05,
                        "soil_mask_orani_5x5": 0.04,
                    }
                ],
            }
        }
    }
    feedback = {
        "kayitlar": [
            {
                "id": "FP",
                "sonuc": "YANLIS_POZITIF",
                "sonuc_tarihi": "2026-09-17",
                "enlem": 38.300000,
                "boylam": 26.300000,
                "eslesme_yaricapi_m": 30,
            }
        ]
    }
    payload = audit(morphology, seeds, feedback)
    region = payload["bolgeler"]["cesme"]
    assert region["coklu_kanit_eslesmesi"] == 1, region
    assert region["kalibrasyonla_engellenen"] == 1, region
    assert region["adaylar"][0]["saha_gorevi"] is False
    assert payload["rota_kapisi_hazir"] is False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("foundation excavation cross evidence self-check: ok")
        return 0
    payload = audit()
    OUTPUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
