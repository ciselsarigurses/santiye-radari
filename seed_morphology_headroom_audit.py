"""Seed-merkezli morfoloji skorundaki 100 puan tavanının etkisini ölçer.

Bu katman yalnız diagnostiktir. Alarm, saha görevi, rota, 250 m² ana üretim eşiği
ve 150–249 m² MİKRO politikası değişmez. Amaç, mevcut toplamsal morfoloji skorunun
100 puanda kırpılması yüzünden ranking bilgisinin kaybolup kaybolmadığını ve tavanı
yalnız diagnostik olarak açmanın saha-doğrulamalı gerçek kazıyı daha iyi sıralayıp
sıralamayacağını ölçmektir.

Gerçek kazı yeni-aday havuzunda aranmaz: kullanıcı talimatı gereği doğrulanmış saha
zaten yeni satış fırsatı değildir. Kalibrasyon puanı `saha_referans_regresyonu`
üzerinden okunur. 100'ün altındaki kırpılmış skor ham skorla aynıdır; 100 olan bir
referansta ham puan bu dosyadan kesin bilinemez.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


INPUT_JSON = Path(__file__).with_name("seed_centered_excavation_morphology_review.json")
OUTPUT_JSON = Path(__file__).with_name("seed_morphology_headroom_review.json")
TRUE_REFERENCE_ID = "FN-20260916-CESME-001"
CAP_SCORE = 100


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


def _reference_regression(region: dict):
    for row in region.get("saha_referans_regresyonu") or []:
        if isinstance(row, dict) and str(row.get("id") or "") == TRUE_REFERENCE_ID:
            return row
    return None


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

    reference = _reference_regression(region) if region_key == "cesme" else None
    reference_capped = None
    reference_raw = None
    reference_raw_exact = False
    reference_detected = False
    reference_regression_ok = False
    reference_seed_distance = None
    if reference:
        try:
            reference_capped = int(reference.get("seed_merkezli_morfoloji_puani"))
        except (TypeError, ValueError):
            reference_capped = None
        reference_detected = reference.get("tespit") is True
        reference_regression_ok = reference.get("regresyon_uyumlu") is True
        reference_seed_distance = reference.get("en_yakin_seed_mesafe_m")
        if reference_capped is not None and 0 < reference_capped < CAP_SCORE:
            reference_raw = reference_capped
            reference_raw_exact = True

    higher_than_reference = (
        sum(1 for value in raw_scores if value > reference_raw)
        if reference_raw_exact else None
    )
    raw_rank_if_inserted = (
        higher_than_reference + 1
        if higher_than_reference is not None else None
    )
    minimum_cap_raw = min(cap_raw_scores) if cap_raw_scores else None
    reference_below_all_cap = bool(
        reference_raw_exact
        and minimum_cap_raw is not None
        and reference_raw < minimum_cap_raw
    )

    return {
        "bolge": region.get("bolge"),
        "onceki_tarih": region.get("onceki_tarih"),
        "son_tarih": region.get("son_tarih"),
        "disari_aktarilan_yeni_aday_sayisi": len(scored),
        "tavan_100_yeni_aday_sayisi": len(capped),
        "kirpilmis_skor_dagilimi": _distribution(
            [int(item.get("seed_merkezli_morfoloji_puani") or 0) for item in scored]
        ),
        "ham_skor_dagilimi": _distribution(raw_scores),
        "tavan_aday_ham_skor_dagilimi": _distribution(cap_raw_scores),
        "tavan_altinda_kaybolan_benzersiz_ham_skor_sayisi": len(set(cap_raw_scores)),
        "gercek_kazi_referansi": {
            "id": TRUE_REFERENCE_ID,
            "regresyonda_var": reference is not None,
            "tespit": reference_detected,
            "regresyon_uyumlu": reference_regression_ok,
            "en_yakin_seed_mesafe_m": reference_seed_distance,
            "kirpilmis_morfoloji_puani": reference_capped,
            "ham_morfoloji_puani": reference_raw,
            "ham_puan_kesin": reference_raw_exact,
            "yeni_aday_havuzuna_eklenmesi_beklenmez": True,
            "ham_skorla_yeni_adaylar_arasina_eklense_sirasi": raw_rank_if_inserted,
            "ham_skorda_referansin_ustundeki_yeni_aday_sayisi": higher_than_reference,
            "tum_100_puan_yeni_adaylarinin_ham_skorundan_dusuk": reference_below_all_cap,
        },
        "yalniz_tavani_acmak_referans_rankingini_kurtarir": bool(
            reference_raw_exact and not reference_below_all_cap
        ) if region_key == "cesme" else None,
        "yorum": (
            "Gerçek kazı referansı 100'ün altında olduğu için ham puanı kesin biliniyor ve tüm "
            "100-puan yeni adaylarının ham skorundan da düşük. Yalnız skor tavanını kaldırmak "
            "ranking/recall sorununu çözmez; aynı-katman pozitiflerinin toplamsal aşırı ödüllendirilmesi "
            "yeniden kalibre edilmelidir."
            if region_key == "cesme" and reference_below_all_cap
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
    ref_ok = bool(
        ref.get("regresyonda_var") is True
        and ref.get("tespit") is True
        and ref.get("regresyon_uyumlu") is True
    )
    ref_raw_exact = ref.get("ham_puan_kesin") is True
    ref_below_caps = ref.get("tum_100_puan_yeni_adaylarinin_ham_skorundan_dusuk") is True

    return {
        "surum": 2,
        "amac": "Morfoloji 100-puan tavanının ranking bilgisini gizleyip gizlemediğini ölçmek",
        "uretim_filtresi": False,
        "alarm": False,
        "saha_gorevi": False,
        "rota_degistirir": False,
        "ana_uretim_esigi_degisti": False,
        "mikro_politikasi_degisti": False,
        "gercek_derinlik_olcumu": False,
        "ham_skor_yalniz_diagnostik": True,
        "dogrulanmis_saha_yeni_aday_sayilmaz": True,
        "gercek_kazi_regresyonu_saglikli": ref_ok,
        "gercek_kazi_ham_puani_kesin": ref_raw_exact,
        "yalniz_tavan_acmak_yeterli": bool(ref_raw_exact and not ref_below_caps),
        "morfoloji_toplamsal_ranking_kalibrasyon_riski": bool(ref_raw_exact and ref_below_caps),
        "bolgeler": regions,
        "operasyonel_yorum": (
            "Gerçek kazı regresyonu yakalanıyor ancak puanı 75; dışa aktarılan 100-puan yeni adayların "
            "ham skorları da 100 veya üzerinde. Sorun yalnız 100 puan kırpması değil, toplamsal morfoloji "
            "kanıtlarının yeni adayları saha-doğrulamalı pozitiften fazla ödüllendirmesi. Üretim eşiği "
            "gevşetilmemeli; morfoloji bağımsız SAR/temporal kanıt olmadan KONTROLE_GIT üretmemeli."
            if ref_raw_exact and ref_below_caps
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

    region = {
        "durum": "ok",
        "saha_referans_regresyonu": [
            {
                "id": TRUE_REFERENCE_ID,
                "tespit": True,
                "regresyon_uyumlu": True,
                "en_yakin_seed_mesafe_m": 11.2,
                "seed_merkezli_morfoloji_puani": 75,
            }
        ],
        "adaylar": [
            {**strong, "seed_merkezli_morfoloji_puani": 100},
        ],
    }
    health = _region_health("cesme", region)
    assert health["gercek_kazi_referansi"]["ham_morfoloji_puani"] == 75
    assert health["gercek_kazi_referansi"]["tum_100_puan_yeni_adaylarinin_ham_skorundan_dusuk"] is True


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
