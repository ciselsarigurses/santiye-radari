"""Seed-merkezli morfoloji skorundaki 100 puan tavanının etkisini ölçer.

Bu katman yalnız diagnostiktir. Alarm, saha görevi, rota, 250 m² ana üretim eşiği
ve 150–249 m² MİKRO politikası değişmez. Amaç, mevcut toplamsal morfoloji skorunun
100 puanda kırpılması yüzünden ranking bilgisinin kaybolup kaybolmadığını ve tavanı
yalnız diagnostik olarak açmanın saha-doğrulamalı gerçek kazıyı daha iyi sıralayıp
sıralamayacağını ölçmektir.

Ham skor burada mevcut seed_centered_excavation_morphology_audit.py kurallarından
tekrar hesaplanır; üretim skoruna veya karar kapılarına yazılmaz.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


INPUT_JSON = Path(__file__).with_name("seed_centered_excavation_morphology_review.json")
OUTPUT_JSON = Path(__file__).with_name("seed_morphology_headroom_review.json")
TRUE_REFERENCE = (38.341846, 26.432861)
TRUE_REFERENCE_ID = "FN-20260916-CESME-001"
MATCH_RADIUS_M = 45.0
CAP_SCORE = 100


def _distance_m(a, b):
    lat1, lon1 = float(a[0]), float(a[1])
    lat2, lon2 = float(b[0]), float(b[1])
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    north = (lat1 - lat2) * 110570.0
    east = (lon1 - lon2) * 111320.0 * math.cos(mean_lat)
    return math.hypot(north, east)


def _raw_score(item: dict) -> int:
    """Mevcut morfoloji skorunu kırpma öncesi aynı kurallarla yeniden hesapla."""
    score = 0
    component_pixels = int(item.get("bilesen_piksel") or 0)
    aspect = item.get("uzun_kisa_orani")
    aspect = float(aspect) if aspect is not None else None
    fill = float(item.get("kutu_doluluk_orani") or 0.0)
    concentration = float(item.get("merkez_dis_konsantrasyon") or 0.0)
    peak_ratio = float(item.get("tepe_merkez_orani") or 0.0)
    core_disturbance = float(item.get("cekirdek_degisim_orani") or 0.0)
    outer_disturbance = float(item.get("dis_cevre_degisim_orani") or 0.0)
    bright_fraction = float(item.get("parlak_zon_orani") or 0.0)
    soil_fraction = float(item.get("soil_orani") or 0.0)
    dark_fraction = float(item.get("koyu_zon_orani") or 0.0)

    if 1 <= component_pixels <= 8:
        score += 20
    elif component_pixels <= 14:
        score += 8
    elif component_pixels > 20:
        score -= 20

    if aspect is not None and aspect <= 3.0:
        score += 10
    elif aspect is not None and aspect >= 5.0:
        score -= 15

    if fill >= 0.40:
        score += 10

    if concentration >= 1.35:
        score += 15
    elif concentration < 1.05:
        score -= 15

    if peak_ratio >= 2.5:
        score += 10

    if 0.04 <= core_disturbance <= 0.45:
        score += 10
    elif core_disturbance >= 0.65:
        score -= 20

    if outer_disturbance <= 0.18:
        score += 15
    elif outer_disturbance >= 0.40:
        score -= 25

    if 0.08 <= bright_fraction <= 0.55:
        score += 10
    elif bright_fraction >= 0.75:
        score -= 20

    if 0.01 <= soil_fraction <= 0.12:
        score += 10
    elif soil_fraction >= 0.25:
        score -= 30

    if 0.02 <= dark_fraction <= 0.35 and bright_fraction >= 0.08:
        score += 5
    elif dark_fraction >= 0.60:
        score -= 20

    return int(round(score))


def _percentile(values, ratio):
    if not values:
        return None
    ordered = sorted(int(v) for v in values)
    index = int(round((len(ordered) - 1) * ratio))
    return ordered[index]


def _distribution(values):
    values = [int(v) for v in values]
    return {
        "sayi": len(values),
        "benzersiz_sayi": len(set(values)),
        "min": min(values) if values else None,
        "p10": _percentile(values, 0.10),
        "medyan": _percentile(values, 0.50),
        "p90": _percentile(values, 0.90),
        "max": max(values) if values else None,
        "aralik": (max(values) - min(values)) if values else None,
    }


def _nearest_true_reference(rows):
    nearest = None
    nearest_distance = None
    for row in rows:
        if "enlem" not in row or "boylam" not in row:
            continue
        distance = _distance_m(TRUE_REFERENCE, (row["enlem"], row["boylam"]))
        if nearest_distance is None or distance < nearest_distance:
            nearest = row
            nearest_distance = distance
    return nearest, nearest_distance


def _region_health(region_key: str, region: dict) -> dict:
    exported = [x for x in (region.get("adaylar") or []) if isinstance(x, dict)]
    scored = []
    for item in exported:
        raw = _raw_score(item)
        scored.append(
            {
                **item,
                "ham_morfoloji_puani": raw,
                "tavan_ustu_headroom": max(raw - CAP_SCORE, 0),
            }
        )

    capped = [
        item for item in scored
        if int(item.get("seed_merkezli_morfoloji_puani") or 0) >= CAP_SCORE
    ]
    raw_scores = [item["ham_morfoloji_puani"] for item in scored]
    cap_raw_scores = [item["ham_morfoloji_puani"] for item in capped]

    nearest, distance = _nearest_true_reference(scored)
    true_match = bool(
        region_key == "cesme"
        and nearest is not None
        and distance is not None
        and distance <= MATCH_RADIUS_M
    )
    true_raw = nearest.get("ham_morfoloji_puani") if true_match else None
    true_capped = (
        int(nearest.get("seed_merkezli_morfoloji_puani") or 0)
        if true_match else None
    )
    higher_than_true = (
        sum(1 for value in raw_scores if value > true_raw)
        if true_raw is not None else None
    )
    raw_rank = (higher_than_true + 1) if higher_than_true is not None else None
    minimum_cap_raw = min(cap_raw_scores) if cap_raw_scores else None
    true_below_all_cap = bool(
        true_raw is not None
        and minimum_cap_raw is not None
        and true_raw < minimum_cap_raw
    )

    return {
        "bolge": region.get("bolge"),
        "onceki_tarih": region.get("onceki_tarih"),
        "son_tarih": region.get("son_tarih"),
        "disari_aktarilan_aday_sayisi": len(scored),
        "tavan_100_aday_sayisi": len(capped),
        "kirpilmis_skor_dagilimi": _distribution(
            [int(item.get("seed_merkezli_morfoloji_puani") or 0) for item in scored]
        ),
        "ham_skor_dagilimi": _distribution(raw_scores),
        "tavan_aday_ham_skor_dagilimi": _distribution(cap_raw_scores),
        "tavan_altinda_kaybolan_benzersiz_ham_skor_sayisi": len(set(cap_raw_scores)),
        "gercek_kazi_referansi": {
            "id": TRUE_REFERENCE_ID,
            "yalniz_bu_bolgede_degerlendirildi": region_key == "cesme",
            "en_yakin_disari_aktarilan_aday_mesafe_m": (
                round(distance, 1) if region_key == "cesme" and distance is not None else None
            ),
            "45m_icinde_eslesme": true_match,
            "kirpilmis_morfoloji_puani": true_capped,
            "ham_morfoloji_puani": true_raw,
            "ham_skor_sirasi": raw_rank,
            "ham_skorda_referansin_ustundeki_aday_sayisi": higher_than_true,
            "tum_tavan_adaylarinin_altinda": true_below_all_cap,
        },
        "yalniz_tavani_acmak_referans_rankingini_kurtarir": bool(
            true_match and not true_below_all_cap
        ) if region_key == "cesme" else None,
        "yorum": (
            "Gerçek kazı referansı tüm 100-puan tavan adaylarının ham skorundan da düşük; "
            "yalnız 100 puan kırpmasını kaldırmak recall/ranking sorununu çözmez. Yeni ayrım "
            "aynı Sentinel-2 toplamsal kanıtlarını daha fazla ödüllendirmek yerine temporal/SAR "
            "ve tarla-negatif bağlamına dayanmalıdır."
            if region_key == "cesme" and true_below_all_cap
            else "Ham skor yalnız diagnostiktir; üretim veya rota kararında kullanılmaz."
        ),
    }


def _build(payload: dict) -> dict:
    regions = {
        key: _region_health(key, value)
        for key, value in (payload.get("bolgeler") or {}).items()
        if isinstance(value, dict) and value.get("durum") == "ok"
    }
    cesme = regions.get("cesme") or {}
    ref = cesme.get("gercek_kazi_referansi") or {}
    true_matched = ref.get("45m_icinde_eslesme") is True
    true_below_caps = ref.get("tum_tavan_adaylarinin_altinda") is True

    return {
        "surum": 1,
        "amac": "Morfoloji 100-puan tavanının ranking bilgisini gizleyip gizlemediğini ölçmek",
        "uretim_filtresi": False,
        "alarm": False,
        "saha_gorevi": False,
        "rota_degistirir": False,
        "ana_uretim_esigi_degisti": False,
        "mikro_politikasi_degisti": False,
        "gercek_derinlik_olcumu": False,
        "ham_skor_yalniz_diagnostik": True,
        "gercek_kazi_referansi_disari_aktarilan_havuzda": true_matched,
        "yalniz_tavan_acmak_yeterli": bool(true_matched and not true_below_caps),
        "bolgeler": regions,
        "operasyonel_yorum": (
            "Gerçek kazı referansı dışa aktarılan havuzda bulunuyor fakat ham toplamsal skorda da "
            "tavan adaylarının altında kalıyor. Sorun yalnız 100 puan kırpması değil; aynı-katman "
            "pozitiflerinin toplamsal doygunluğu. Üretim eşiği gevşetilmemeli ve morfoloji tek başına "
            "KONTROLE_GIT üretmemeli."
            if true_matched and true_below_caps
            else "Bu ölçüm üretim kararını değiştirmez; ham skor yalnız diagnostiktir."
        ),
    }


def _self_check():
    strong = {
        "bilesen_piksel": 3,
        "uzun_kisa_orani": 2.0,
        "kutu_doluluk_orani": 0.8,
        "merkez_dis_konsantrasyon": 1.8,
        "tepe_merkez_orani": 3.0,
        "cekirdek_degisim_orani": 0.20,
        "dis_cevre_degisim_orani": 0.10,
        "parlak_zon_orani": 0.30,
        "soil_orani": 0.08,
        "koyu_zon_orani": 0.10,
    }
    assert _raw_score(strong) == 115

    broad = {
        "bilesen_piksel": 25,
        "uzun_kisa_orani": 6.0,
        "kutu_doluluk_orani": 0.2,
        "merkez_dis_konsantrasyon": 0.9,
        "tepe_merkez_orani": 1.2,
        "cekirdek_degisim_orani": 0.80,
        "dis_cevre_degisim_orani": 0.50,
        "parlak_zon_orani": 0.90,
        "soil_orani": 0.50,
        "koyu_zon_orani": 0.70,
    }
    assert _raw_score(broad) < 0
    dist = _distribution([75, 100, 105, 115])
    assert dist["min"] == 75 and dist["max"] == 115 and dist["aralik"] == 40


def audit() -> dict:
    payload = json.loads(INPUT_JSON.read_text(encoding="utf-8"))
    return _build(payload)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("seed morphology headroom self-check: ok")
        return

    result = audit()
    OUTPUT_JSON.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
