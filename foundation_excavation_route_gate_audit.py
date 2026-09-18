"""Temel/kepçe kazısı için konservatif çoklu-kanıt rota kapısını denetler.

Bu katman yalnız diagnostiktir; doğrudan alarm veya saha görevi üretmez. Sentinel-2
10 m veriden gerçek kazı derinliği ölçtüğünü iddia etmez. Amaç, lokal-seed merkezli
morfoloji ile zamansal olarak uygun Sentinel-1 RTC lokal desteğini aynı koordinatta
birleştirmek ve saha geri bildirimlerini kapı öncesinde uygulamaktır.

250 m² ana eşik korunur. 150–249 m² MİKRO yalnız diagnostik kalır. Bilinen yanlış
pozitifler aynı/eski optik sahneyle geri gelemez; mevcut müşteri yeni satış fırsatı
sayılmaz. Rota kapısı, saha kalibrasyonu yeterli olana kadar kapalıdır. Doğrulanmış
kazı kalibrasyonu yalnız saha doğrulama tarihine eşit veya daha yeni Sentinel-2
sahnesiyle zamansal olarak geçerli hale gelmiş referanslardan sayılır.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path


BASE = Path(__file__).resolve().parent
MORPHOLOGY_JSON = BASE / "seed_centered_excavation_morphology_review.json"
SAR_JSON = BASE / "foundation_excavation_sar_seed_review.json"
FEEDBACK_JSON = BASE / "manual_field_feedback.json"
REFERENCE_SIMILARITY_JSON = BASE / "foundation_excavation_reference_similarity_review.json"
OUTPUT_JSON = BASE / "foundation_excavation_route_gate_review.json"

MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
MATCH_RADIUS_M = 45.0
MIN_MORPHOLOGY_SCORE = 65
MIN_CONFIRMED_EXCAVATIONS_FOR_ROUTE = 2


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
    lat1, lon1 = float(a[0]), float(a[1])
    lat2, lon2 = float(b[0]), float(b[1])
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _date(value):
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
    return [
        item for item in (payload.get("kayitlar") or [])
        if isinstance(item, dict) and _point(item) is not None
    ]


def _feedback_effect(candidate, scene_date, feedback):
    matches = []
    for item in feedback:
        radius = float(item.get("eslesme_yaricapi_m") or 30)
        distance = _distance_m(_point(candidate), _point(item))
        if distance <= max(radius, MATCH_RADIUS_M):
            matches.append((distance, item))
    if not matches:
        return {
            "engel": False,
            "mevcut_musteri": False,
            "geri_bildirim": None,
        }

    distance, item = min(matches, key=lambda row: row[0])
    outcome = str(item.get("sonuc") or "").upper()
    feedback_date = _date(item.get("sonuc_tarihi"))
    same_or_older_scene = bool(
        scene_date is not None
        and feedback_date is not None
        and scene_date <= feedback_date
    )
    false_positive_block = outcome == "YANLIS_POZITIF" and same_or_older_scene
    existing_customer = outcome == "MEVCUT_MUSTERI"
    return {
        "engel": bool(false_positive_block or existing_customer),
        "mevcut_musteri": existing_customer,
        "geri_bildirim": {
            "id": item.get("id"),
            "sonuc": outcome,
            "mesafe_m": round(distance, 1),
            "sonuc_tarihi": item.get("sonuc_tarihi"),
        },
    }


def _sar_records(region):
    return [
        item for item in (region.get("tum_sar_sonuclari") or [])
        if isinstance(item, dict) and _point(item) is not None
    ]


def _candidate_area(candidate):
    try:
        return int(candidate.get("spektral_etki_alani_m2") or 0)
    except (TypeError, ValueError):
        return 0


def _analyze_region(region_key, morphology_region, sar_region, feedback):
    scene_date = _date(morphology_region.get("son_tarih"))
    sar_rows = _sar_records(sar_region)
    rows = []

    for candidate in morphology_region.get("adaylar") or []:
        if not isinstance(candidate, dict) or _point(candidate) is None:
            continue
        try:
            morphology_score = float(candidate.get("seed_merkezli_morfoloji_puani") or 0)
        except (TypeError, ValueError):
            morphology_score = 0.0
        morphology_level = str(candidate.get("seed_merkezli_morfoloji_seviyesi") or "").upper()
        morphology_ok = morphology_score >= MIN_MORPHOLOGY_SCORE and morphology_level == "YUKSEK"

        sar, sar_distance = _nearest(candidate, sar_rows)
        sar_ok = bool(
            sar is not None
            and sar_distance is not None
            and sar_distance <= MATCH_RADIUS_M
            and sar.get("s2_sar_capraz_destek") is True
        )

        area_m2 = _candidate_area(candidate)
        main_band = area_m2 >= MAIN_THRESHOLD_M2
        micro_band = MICRO_RANGE_M2[0] <= area_m2 <= MICRO_RANGE_M2[1]
        feedback_effect = _feedback_effect(candidate, scene_date, feedback)
        multi_evidence = bool(morphology_ok and sar_ok)
        high_confidence_diagnostic = bool(
            multi_evidence and main_band and not feedback_effect["engel"]
        )
        micro_diagnostic = bool(
            multi_evidence and micro_band and not feedback_effect["engel"]
        )

        rows.append({
            "enlem": candidate.get("enlem"),
            "boylam": candidate.get("boylam"),
            "spektral_etki_alani_m2": area_m2,
            "morfoloji_puani": morphology_score,
            "morfoloji_yuksek": morphology_ok,
            "sar_mesafe_m": round(sar_distance, 1) if sar_distance is not None else None,
            "sar_capraz_destek": sar_ok,
            "sar_lokal_degisim_skor_db": sar.get("sar_lokal_degisim_skor_db") if sar else None,
            "coklu_kanit": multi_evidence,
            "ana_esik": main_band,
            "mikro_diagnostik": micro_diagnostic,
            "yuksek_guven_diagnostik": high_confidence_diagnostic,
            "mevcut_musteri": feedback_effect["mevcut_musteri"],
            "geri_bildirim_engeli": feedback_effect["engel"],
            "geri_bildirim": feedback_effect["geri_bildirim"],
            "alarm": False,
            "saha_gorevi": False,
        })

    rows.sort(
        key=lambda item: (
            bool(item["yuksek_guven_diagnostik"]),
            bool(item["coklu_kanit"]),
            float(item.get("morfoloji_puani") or 0),
        ),
        reverse=True,
    )
    return {
        "bolge": morphology_region.get("bolge") or region_key,
        "s2_son_tarih": morphology_region.get("son_tarih"),
        "sar_yeni_tarih": sar_region.get("sar_yeni_tarih"),
        "sar_optik_doneme_zamansal_uygun": sar_region.get("sar_optik_doneme_zamansal_uygun"),
        "aday_sayisi": len(rows),
        "coklu_kanit_sayisi": sum(1 for item in rows if item["coklu_kanit"]),
        "ana_esik_yuksek_guven_sayisi": sum(1 for item in rows if item["yuksek_guven_diagnostik"]),
        "mikro_coklu_kanit_sayisi": sum(1 for item in rows if item["mikro_diagnostik"]),
        "adaylar": rows,
    }


def audit(morphology=None, sar=None, feedback=None, reference_similarity=None):
    morphology = morphology if isinstance(morphology, dict) else _load(MORPHOLOGY_JSON)
    sar = sar if isinstance(sar, dict) else _load(SAR_JSON)
    feedback = feedback if isinstance(feedback, dict) else _load(FEEDBACK_JSON)
    reference_similarity = (
        reference_similarity
        if isinstance(reference_similarity, dict)
        else _load(REFERENCE_SIMILARITY_JSON)
    )
    feedback_rows = _feedback_records(feedback)

    regions = {}
    for region_key in ("cesme", "uzunkuyu"):
        regions[region_key] = _analyze_region(
            region_key,
            (morphology.get("bolgeler") or {}).get(region_key) or {},
            (sar.get("bolgeler") or {}).get(region_key) or {},
            feedback_rows,
        )

    field_confirmed_excavations = sum(
        1 for item in feedback_rows
        if str(item.get("sonuc") or "").upper() == "DOGRULANMIS_KAZI"
    )
    try:
        confirmed_excavations = int(reference_similarity.get("dogrulanmis_kazi_referans_sayisi") or 0)
    except (TypeError, ValueError):
        confirmed_excavations = 0
    try:
        temporally_invalid_excavations = int(
            reference_similarity.get("zamansal_gecersiz_dogrulanmis_kazi_referans_sayisi") or 0
        )
    except (TypeError, ValueError):
        temporally_invalid_excavations = 0

    total_high = sum(region["ana_esik_yuksek_guven_sayisi"] for region in regions.values())
    route_ready = bool(
        confirmed_excavations >= MIN_CONFIRMED_EXCAVATIONS_FOR_ROUTE
        and int(morphology.get("toplam_regresyon_uyumsuz") or 0) == 0
        and total_high > 0
    )
    return {
        "surum": 2,
        "amac": "Lokal temel/kepçe morfolojisi + zamansal uygun SAR + saha kalibrasyonu ile konservatif rota kapısı diagnostigi",
        "gercek_derinlik_olcumu": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "saha_dogrulanmis_kazi_sayisi": field_confirmed_excavations,
        "dogrulanmis_kazi_referansi": confirmed_excavations,
        "zamansal_gecersiz_dogrulanmis_kazi_referansi": temporally_invalid_excavations,
        "rota_kapisi_icin_min_dogrulanmis_kazi": MIN_CONFIRMED_EXCAVATIONS_FOR_ROUTE,
        "toplam_yuksek_guven_diagnostik": total_high,
        "rota_kapisi_hazir": route_ready,
        "bolgeler": regions,
        "not": (
            "Rota kapısı yalnız diagnostiktir. En az iki zamansal geçerli doğrulanmış kazı referansı, sıfır "
            "seed-merkezli morfoloji regresyon uyumsuzluğu ve aynı koordinatta zamansal uygun Sentinel-1 "
            "çapraz desteği olmadan saha görevi üretilmez. Saha doğrulaması tek başına kalibrasyon referansı "
            "sayılmaz; Sentinel-2 son sahnesi doğrulama tarihine yetişmelidir."
        ),
    }


def _self_check():
    morphology = {
        "toplam_regresyon_uyumsuz": 0,
        "bolgeler": {
            "cesme": {
                "son_tarih": "15.09.2026",
                "adaylar": [
                    {
                        "enlem": 38.30,
                        "boylam": 26.30,
                        "spektral_etki_alani_m2": 400,
                        "seed_merkezli_morfoloji_puani": 80,
                        "seed_merkezli_morfoloji_seviyesi": "YUKSEK",
                    },
                    {
                        "enlem": 38.31,
                        "boylam": 26.31,
                        "spektral_etki_alani_m2": 200,
                        "seed_merkezli_morfoloji_puani": 80,
                        "seed_merkezli_morfoloji_seviyesi": "YUKSEK",
                    },
                ],
            },
            "uzunkuyu": {"son_tarih": "15.09.2026", "adaylar": []},
        },
    }
    sar = {
        "bolgeler": {
            "cesme": {
                "sar_yeni_tarih": "2026-09-15",
                "sar_optik_doneme_zamansal_uygun": True,
                "tum_sar_sonuclari": [
                    {
                        "enlem": 38.30,
                        "boylam": 26.30,
                        "s2_sar_capraz_destek": True,
                        "sar_lokal_degisim_skor_db": 2.4,
                    },
                    {
                        "enlem": 38.31,
                        "boylam": 26.31,
                        "s2_sar_capraz_destek": True,
                        "sar_lokal_degisim_skor_db": 2.6,
                    },
                ],
            },
            "uzunkuyu": {"tum_sar_sonuclari": []},
        }
    }
    feedback = {
        "kayitlar": [
            {
                "id": "TP1",
                "sonuc": "DOGRULANMIS_KAZI",
                "sonuc_tarihi": "2026-09-14",
                "enlem": 38.29,
                "boylam": 26.29,
                "eslesme_yaricapi_m": 25,
            },
            {
                "id": "TP2",
                "sonuc": "DOGRULANMIS_KAZI",
                "sonuc_tarihi": "2026-09-14",
                "enlem": 38.28,
                "boylam": 26.28,
                "eslesme_yaricapi_m": 25,
            },
        ]
    }
    valid_references = {
        "dogrulanmis_kazi_referans_sayisi": 2,
        "zamansal_gecersiz_dogrulanmis_kazi_referans_sayisi": 0,
    }
    payload = audit(morphology, sar, feedback, valid_references)
    assert payload["bolgeler"]["cesme"]["ana_esik_yuksek_guven_sayisi"] == 1
    assert payload["bolgeler"]["cesme"]["mikro_coklu_kanit_sayisi"] == 1
    assert payload["saha_dogrulanmis_kazi_sayisi"] == 2
    assert payload["dogrulanmis_kazi_referansi"] == 2
    assert payload["rota_kapisi_hazir"] is True

    temporally_invalid_references = {
        "dogrulanmis_kazi_referans_sayisi": 0,
        "zamansal_gecersiz_dogrulanmis_kazi_referans_sayisi": 2,
    }
    payload = audit(morphology, sar, feedback, temporally_invalid_references)
    assert payload["saha_dogrulanmis_kazi_sayisi"] == 2
    assert payload["dogrulanmis_kazi_referansi"] == 0
    assert payload["zamansal_gecersiz_dogrulanmis_kazi_referansi"] == 2
    assert payload["rota_kapisi_hazir"] is False

    feedback["kayitlar"].append(
        {
            "id": "FP",
            "sonuc": "YANLIS_POZITIF",
            "sonuc_tarihi": "2026-09-15",
            "enlem": 38.30,
            "boylam": 26.30,
            "eslesme_yaricapi_m": 25,
        }
    )
    payload = audit(morphology, sar, feedback, valid_references)
    assert payload["bolgeler"]["cesme"]["ana_esik_yuksek_guven_sayisi"] == 0
    assert payload["rota_kapisi_hazir"] is False

    no_sar = json.loads(json.dumps(sar))
    no_sar["bolgeler"]["cesme"]["tum_sar_sonuclari"][0]["s2_sar_capraz_destek"] = False
    payload = audit(
        morphology,
        no_sar,
        {"kayitlar": feedback["kayitlar"][:2]},
        valid_references,
    )
    assert payload["bolgeler"]["cesme"]["ana_esik_yuksek_guven_sayisi"] == 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    _self_check()
    if args.check_only:
        print("foundation excavation route gate self-check: ok")
        return
    payload = audit()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
