"""Temel/kepçe adaylarında SAR'ı pozitif-only kanıt olarak kullanan recall diagnostiği.

Bu katman yalnız diagnostiktir; alarm, saha görevi veya operasyonel rota üretmez.
Sentinel-2 10 m veriden gerçek kazı derinliği ölçmez. Amaç mevcut konservatif
rota-kapısının SAR desteğini fiilen zorunlu kılmasından doğan recall körlüğünü,
SAR yokluğunu negatif kanıt saymadan ölçmektir.

İkinci bağımsız/zamansal destek olarak iki yol kabul edilir:
- zamansal olarak uygun güçlü Sentinel-1 lokal desteği; veya
- daha eski tarihte kaydedilmiş, aynı noktadaki güçlü temporal-lokal Sentinel-2 izi.

250 m² ana eşik korunur. 150–249 m² MİKRO yalnız diagnostik kalır. Mevcut müşteri,
saha yanlış-pozitifi, tarla/şerit negatif bağlamı ve spektral FP-klon baskısı
pozitif recall hesabında zorunlu negatif kapı olarak korunur.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path


BASE = Path(__file__).resolve().parent
ROUTE_JSON = BASE / "foundation_excavation_route_gate_review.json"
TEMPORAL_JSON = BASE / "temporal_local_precursor_watchlist.json"
SAR_CAL_JSON = BASE / "sar_hard_veto_calibration_review.json"
OUTPUT_JSON = BASE / "foundation_excavation_positive_evidence_review.json"

MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
TEMPORAL_MATCH_RADIUS_M = 25.0


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
        record_point = _point(record)
        if record_point is None:
            continue
        distance = _distance_m(target_point, record_point)
        if nearest_distance is None or distance < nearest_distance:
            nearest = record
            nearest_distance = distance
    return nearest, nearest_distance


def _temporal_support(candidate, scene_date, precursors):
    precursor, distance = _nearest(candidate, precursors)
    if precursor is None or distance is None or distance > TEMPORAL_MATCH_RADIUS_M:
        return False, precursor, distance
    precursor_date = _date(
        precursor.get("son_guclu_tarih") or precursor.get("ilk_guclu_tarih")
    )
    # Aynı sahne kanıtını iki kez sayma: öncül tarih mutlaka mevcut morfoloji
    # sahnesinden daha eski olmalı.
    valid = bool(scene_date and precursor_date and precursor_date < scene_date)
    return valid, precursor, distance


def _negative_context_reasons(candidate):
    reasons = []
    if candidate.get("geri_bildirim_engeli") is True:
        reasons.append("SAHA_GERI_BILDIRIM_ENGELI")
    if candidate.get("mevcut_musteri") is True:
        reasons.append("MEVCUT_MUSTERI")
    if candidate.get("tarla_bahce_baskilandi") is True:
        reasons.append("TARLA_BAHCE_NEGATIF_BAGLAM")
    if candidate.get("yanlis_pozitif_spektral_klon_baskilandi") is True:
        reasons.append("YANLIS_POZITIF_SPEKTRAL_KLON")
    return reasons


def _analyze_region(region_key, route_region, precursors):
    scene_date = _date(route_region.get("s2_son_tarih"))
    region_precursors = [
        item
        for item in precursors
        if isinstance(item, dict)
        and item.get("bolge_anahtari") == region_key
        and _point(item) is not None
    ]
    rows = []
    for candidate in route_region.get("adaylar") or []:
        if not isinstance(candidate, dict) or _point(candidate) is None:
            continue
        try:
            area_m2 = int(candidate.get("spektral_etki_alani_m2") or 0)
        except (TypeError, ValueError):
            area_m2 = 0

        morphology_ok = candidate.get("morfoloji_yuksek") is True
        sar_positive = candidate.get("sar_capraz_destek") is True
        temporal_positive, precursor, precursor_distance = _temporal_support(
            candidate, scene_date, region_precursors
        )
        temporal_in_radius = bool(
            precursor is not None
            and precursor_distance is not None
            and precursor_distance <= TEMPORAL_MATCH_RADIUS_M
        )
        negative_reasons = _negative_context_reasons(candidate)
        blocked = bool(negative_reasons)
        independent_positive = bool(sar_positive or temporal_positive)
        multi_positive = bool(morphology_ok and independent_positive and not blocked)
        main_band = area_m2 >= MAIN_THRESHOLD_M2
        micro_band = MICRO_RANGE_M2[0] <= area_m2 <= MICRO_RANGE_M2[1]
        main_supported = bool(multi_positive and main_band)
        micro_supported = bool(multi_positive and micro_band)
        three_axis = bool(
            morphology_ok and sar_positive and temporal_positive and not blocked
        )

        sources = []
        if morphology_ok:
            sources.append("S2_MORFOLOJI")
        if sar_positive:
            sources.append("S1_LOKAL_POZITIF")
        if temporal_positive:
            sources.append("S2_ONCUL_TEMPORAL")

        rows.append(
            {
                "enlem": candidate.get("enlem"),
                "boylam": candidate.get("boylam"),
                "spektral_etki_alani_m2": area_m2,
                "morfoloji_puani": candidate.get("morfoloji_puani"),
                "morfoloji_yuksek": morphology_ok,
                "sar_pozitif_destek": sar_positive,
                "sar_lokal_degisim_skor_db": candidate.get("sar_lokal_degisim_skor_db"),
                "sar_yoklugu_negatif_kanit": False,
                "temporal_oncul_destek": temporal_positive,
                "temporal_oncul_esik_icinde": temporal_in_radius,
                "temporal_oncul_mesafe_m": (
                    round(precursor_distance, 1) if temporal_in_radius else None
                ),
                "temporal_en_yakin_mesafe_m": (
                    round(precursor_distance, 1)
                    if precursor_distance is not None
                    else None
                ),
                "temporal_iz_id": (
                    precursor.get("temporal_iz_id")
                    if precursor is not None and temporal_in_radius
                    else None
                ),
                "temporal_oncul_tarih": (
                    precursor.get("son_guclu_tarih")
                    or precursor.get("ilk_guclu_tarih")
                    if precursor is not None and temporal_in_radius
                    else None
                ),
                "pozitif_kanit_kaynaklari": sources,
                "bagimsiz_pozitif_destek": independent_positive,
                "negatif_baglamsal_engel": blocked,
                "negatif_baglamsal_nedenler": negative_reasons,
                "coklu_pozitif_kanit": multi_positive,
                "ana_esik": main_band,
                "ana_esik_pozitif_destekli_diagnostik": main_supported,
                "mikro_pozitif_destekli_diagnostik": micro_supported,
                "uc_eksenli_destek": three_axis,
                "geri_bildirim_engeli": candidate.get("geri_bildirim_engeli") is True,
                "mevcut_musteri": candidate.get("mevcut_musteri") is True,
                "tarla_bahce_baskilandi": candidate.get("tarla_bahce_baskilandi") is True,
                "yanlis_pozitif_spektral_klon_baskilandi": (
                    candidate.get("yanlis_pozitif_spektral_klon_baskilandi") is True
                ),
                "alarm": False,
                "saha_gorevi": False,
            }
        )

    rows.sort(
        key=lambda item: (
            bool(item["ana_esik_pozitif_destekli_diagnostik"]),
            bool(item["uc_eksenli_destek"]),
            bool(item["coklu_pozitif_kanit"]),
            -bool(item["negatif_baglamsal_engel"]),
            float(item.get("morfoloji_puani") or 0),
        ),
        reverse=True,
    )
    return {
        "bolge": route_region.get("bolge") or region_key,
        "s2_son_tarih": route_region.get("s2_son_tarih"),
        "aday_sayisi": len(rows),
        "negatif_baglamsal_baskilanan_sayi": sum(
            1 for item in rows if item["negatif_baglamsal_engel"]
        ),
        "ana_esik_pozitif_destekli_sayi": sum(
            1 for item in rows if item["ana_esik_pozitif_destekli_diagnostik"]
        ),
        "mikro_pozitif_destekli_sayi": sum(
            1 for item in rows if item["mikro_pozitif_destekli_diagnostik"]
        ),
        "uc_eksenli_destekli_sayi": sum(
            1 for item in rows if item["uc_eksenli_destek"]
        ),
        "adaylar": rows,
    }


def audit(route=None, temporal=None, sar_calibration=None):
    route = route if isinstance(route, dict) else _load(ROUTE_JSON)
    temporal = temporal if isinstance(temporal, dict) else _load(TEMPORAL_JSON)
    sar_calibration = (
        sar_calibration
        if isinstance(sar_calibration, dict)
        else _load(SAR_CAL_JSON)
    )
    precursors = [
        item
        for item in (temporal.get("adaylar") or [])
        if isinstance(item, dict) and _point(item) is not None
    ]
    regions = {}
    for region_key in ("cesme", "uzunkuyu"):
        regions[region_key] = _analyze_region(
            region_key,
            (route.get("bolgeler") or {}).get(region_key) or {},
            precursors,
        )

    total_main = sum(
        x["ana_esik_pozitif_destekli_sayi"] for x in regions.values()
    )
    total_micro = sum(
        x["mikro_pozitif_destekli_sayi"] for x in regions.values()
    )
    total_three = sum(
        x["uc_eksenli_destekli_sayi"] for x in regions.values()
    )
    total_negative = sum(
        x["negatif_baglamsal_baskilanan_sayi"] for x in regions.values()
    )
    sar_veto_available = (
        sar_calibration.get("sar_zorunlu_veto_kullanilabilir") is True
    )
    return {
        "surum": 3,
        "amac": (
            "SAR yokluğunu veto yapmadan morfoloji + S1 pozitif veya önceki-tarih "
            "temporal-lokal kanıtını çaprazlamak; tarla/FP negatif kapılarını korumak"
        ),
        "gercek_derinlik_olcumu": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "rota_kapisi_hazir": False,
        "sar_zorunlu_veto_kullanilabilir": sar_veto_available,
        "sar_yoklugu_negatif_kanit": False,
        "tarla_bahce_negatif_kapi_korunuyor": True,
        "yanlis_pozitif_klon_negatif_kapi_korunuyor": True,
        "temporal_eslesme_yaricapi_m": TEMPORAL_MATCH_RADIUS_M,
        "toplam_negatif_baglamsal_baskilanan": total_negative,
        "toplam_ana_esik_pozitif_destekli_diagnostik": total_main,
        "toplam_mikro_pozitif_destekli_diagnostik": total_micro,
        "toplam_uc_eksenli_destekli_diagnostik": total_three,
        "bolgeler": regions,
        "not": (
            "Bu çıktı yalnız recall diagnostiğidir. SAR güçlü-lokal desteği pozitif "
            "kanıttır; SAR yokluğu negatif veto değildir. Önceki tarih temporal-lokal "
            "iz yalnız mevcut morfoloji adayıyla 25 m içinde ve daha eski bir sahnede "
            "ise ikinci kanıt sayılır. 25 m dışındaki en yakın iz yalnız debug mesafesi "
            "olarak tutulur; temporal_iz_id/tarih ile aday arasında ilişki kurulmaz. "
            "Saha yanlış-pozitifi, mevcut müşteri, tarla/bahçe negatif bağlamı ve "
            "spektral FP-klon baskısı pozitif recall sayımını veto eder. 250 m² ana eşik "
            "ve 150–249 m² MİKRO politikası değişmez; bu katman otomatik saha görevi üretmez."
        ),
    }


def _self_check():
    route = {
        "bolgeler": {
            "cesme": {
                "s2_son_tarih": "18.09.2026",
                "adaylar": [
                    {
                        "enlem": 38.30,
                        "boylam": 26.30,
                        "spektral_etki_alani_m2": 400,
                        "morfoloji_puani": 90,
                        "morfoloji_yuksek": True,
                        "sar_capraz_destek": False,
                    },
                    {
                        "enlem": 38.31,
                        "boylam": 26.31,
                        "spektral_etki_alani_m2": 400,
                        "morfoloji_puani": 90,
                        "morfoloji_yuksek": True,
                        "sar_capraz_destek": False,
                    },
                    {
                        "enlem": 38.32,
                        "boylam": 26.32,
                        "spektral_etki_alani_m2": 500,
                        "morfoloji_puani": 88,
                        "morfoloji_yuksek": True,
                        "sar_capraz_destek": True,
                    },
                    {
                        "enlem": 38.33,
                        "boylam": 26.33,
                        "spektral_etki_alani_m2": 200,
                        "morfoloji_puani": 85,
                        "morfoloji_yuksek": True,
                        "sar_capraz_destek": False,
                    },
                    {
                        "enlem": 38.34,
                        "boylam": 26.34,
                        "spektral_etki_alani_m2": 400,
                        "morfoloji_puani": 95,
                        "morfoloji_yuksek": True,
                        "sar_capraz_destek": True,
                        "yanlis_pozitif_spektral_klon_baskilandi": True,
                    },
                    {
                        "enlem": 38.35,
                        "boylam": 26.35,
                        "spektral_etki_alani_m2": 400,
                        "morfoloji_puani": 95,
                        "morfoloji_yuksek": True,
                        "sar_capraz_destek": False,
                        "geri_bildirim_engeli": True,
                    },
                    {
                        "enlem": 38.36,
                        "boylam": 26.36,
                        "spektral_etki_alani_m2": 500,
                        "morfoloji_puani": 96,
                        "morfoloji_yuksek": True,
                        "sar_capraz_destek": True,
                        "tarla_bahce_baskilandi": True,
                    },
                ],
            },
            "uzunkuyu": {"s2_son_tarih": "18.09.2026", "adaylar": []},
        }
    }
    temporal = {
        "adaylar": [
            {
                "temporal_iz_id": "T1",
                "enlem": 38.30001,
                "boylam": 26.30001,
                "bolge_anahtari": "cesme",
                "son_guclu_tarih": "15.09.2026",
            },
            {
                "temporal_iz_id": "T2",
                "enlem": 38.33001,
                "boylam": 26.33001,
                "bolge_anahtari": "cesme",
                "son_guclu_tarih": "15.09.2026",
            },
            {
                "temporal_iz_id": "T3",
                "enlem": 38.35001,
                "boylam": 26.35001,
                "bolge_anahtari": "cesme",
                "son_guclu_tarih": "15.09.2026",
            },
        ]
    }
    sar_calibration = {"sar_zorunlu_veto_kullanilabilir": False}
    payload = audit(route, temporal, sar_calibration)
    rows = payload["bolgeler"]["cesme"]["adaylar"]
    by_lat = {round(float(item["enlem"]), 2): item for item in rows}

    assert by_lat[38.30]["ana_esik_pozitif_destekli_diagnostik"] is True
    assert by_lat[38.30]["sar_yoklugu_negatif_kanit"] is False
    assert by_lat[38.30]["temporal_oncul_esik_icinde"] is True
    assert by_lat[38.30]["temporal_iz_id"] == "T1"
    assert by_lat[38.31]["ana_esik_pozitif_destekli_diagnostik"] is False
    assert by_lat[38.31]["temporal_oncul_esik_icinde"] is False
    assert by_lat[38.31]["temporal_oncul_mesafe_m"] is None
    assert by_lat[38.31]["temporal_iz_id"] is None
    assert float(by_lat[38.31]["temporal_en_yakin_mesafe_m"]) > TEMPORAL_MATCH_RADIUS_M
    assert by_lat[38.32]["ana_esik_pozitif_destekli_diagnostik"] is True
    assert by_lat[38.33]["mikro_pozitif_destekli_diagnostik"] is True

    # Güçlü SAR olsa bile saha/FP/tarla negatif kapıları recall katmanında aşılmaz.
    assert by_lat[38.34]["ana_esik_pozitif_destekli_diagnostik"] is False
    assert by_lat[38.34]["negatif_baglamsal_engel"] is True
    assert "YANLIS_POZITIF_SPEKTRAL_KLON" in by_lat[38.34]["negatif_baglamsal_nedenler"]
    assert by_lat[38.35]["ana_esik_pozitif_destekli_diagnostik"] is False
    assert by_lat[38.35]["negatif_baglamsal_engel"] is True
    assert by_lat[38.36]["ana_esik_pozitif_destekli_diagnostik"] is False
    assert by_lat[38.36]["negatif_baglamsal_engel"] is True
    assert "TARLA_BAHCE_NEGATIF_BAGLAM" in by_lat[38.36]["negatif_baglamsal_nedenler"]

    assert payload["tarla_bahce_negatif_kapi_korunuyor"] is True
    assert payload["yanlis_pozitif_klon_negatif_kapi_korunuyor"] is True
    assert payload["sar_zorunlu_veto_kullanilabilir"] is False
    assert payload["rota_kapisi_hazir"] is False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("foundation excavation positive-evidence recall self-check: ok")
        return
    payload = audit()
    OUTPUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
