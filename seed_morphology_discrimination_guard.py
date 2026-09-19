"""Seed-merkezli morfoloji skorunun ayırt ediciliğini diagnostik olarak denetler.

Bu guard üretim filtresi değildir. Alarm, saha görevi, rota veya 250 m² / 150–249 m²
politikalarını değiştirmez. Amaç, aynı Sentinel-2 türevi kanıtların toplanması nedeniyle
morfoloji skorlarının kitlesel olarak ORTA/YÜKSEK seviyeye doygunlaşıp doygunlaşmadığını
ölçmek ve böyle bir durumda skorun tek başına güven kanıtı sayılmasını engelleyecek
ölçülebilir bir sağlık sinyali üretmektir.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


INPUT_JSON = Path(__file__).with_name("seed_centered_excavation_morphology_review.json")
OUTPUT_JSON = Path(__file__).with_name("seed_morphology_discrimination_review.json")

SATURATION_RATIO = 0.95
MIN_SEEDS = 20
CORE_POSITIVE_ID = "FN-20260916-CESME-001"
CORE_FALSE_POSITIVE_IDS = {
    "FP-20260916-REISDERE-001",
    "FP-20260917-UZUNKUYU-001",
    "FP-20260917-MUSALLA-001",
    "FP-20260917-MUSALLA-002",
}
CORE_IDS = {CORE_POSITIVE_ID, *CORE_FALSE_POSITIVE_IDS}


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return float(numerator) / float(denominator)


def _region_health(region: dict) -> dict:
    total = int(region.get("lokal_seed_sayisi") or 0)
    medium_high = int(region.get("orta_yuksek_seed_morfoloji") or 0)
    medium_high_ratio = _ratio(medium_high, total)

    exported = [x for x in (region.get("adaylar") or []) if isinstance(x, dict)]
    capped = sum(
        1 for item in exported
        if int(item.get("seed_merkezli_morfoloji_puani") or 0) >= 100
    )
    capped_ratio = _ratio(capped, len(exported))

    saturation = bool(total >= MIN_SEEDS and medium_high_ratio >= SATURATION_RATIO)
    return {
        "lokal_seed_sayisi": total,
        "orta_yuksek_seed_morfoloji": medium_high,
        "orta_yuksek_orani": round(medium_high_ratio, 4),
        "disari_aktarilan_aday_sayisi": len(exported),
        "tavan_100_puanli_disari_aktarilan": capped,
        "tavan_100_puan_orani": round(capped_ratio, 4),
        "skor_doygunlugu_riski": saturation,
        "tek_basina_guven_kaniti_olarak_kullanilabilir": False if saturation else True,
        "not": (
            "Morfoloji seviyesi aday havuzunun neredeyse tamamını ORTA/YUKSEK işaretliyor; "
            "bu katman ayırt edici değildir ve bağımsız SAR/temporal/yapılaşma kanıtı olmadan "
            "yüksek güven veya KONTROLE_GIT üretmemelidir."
            if saturation
            else "Morfoloji seviyesi bu sağlık eşiğinde doygun görünmüyor."
        ),
    }


def _core_regression(regions: dict) -> dict:
    rows = {}
    for region in regions.values():
        if not isinstance(region, dict):
            continue
        for item in region.get("saha_referans_regresyonu") or []:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if item_id in CORE_IDS:
                rows[item_id] = item

    missing = sorted(CORE_IDS - set(rows))
    positive_ok = bool(
        CORE_POSITIVE_ID in rows
        and rows[CORE_POSITIVE_ID].get("regresyon_uyumlu") is True
        and rows[CORE_POSITIVE_ID].get("tespit") is True
    )
    false_positive_failures = sorted(
        item_id
        for item_id in CORE_FALSE_POSITIVE_IDS
        if item_id in rows
        and (
            rows[item_id].get("regresyon_uyumlu") is not True
            or rows[item_id].get("tespit") is True
        )
    )
    return {
        "cekirdek_referans_sayisi": len(CORE_IDS),
        "bulunan_cekirdek_referans_sayisi": len(rows),
        "eksik_cekirdek_referanslar": missing,
        "gercek_kazi_referansi_korundu": positive_ok,
        "yanlis_pozitif_regresyon_ihlalleri": false_positive_failures,
        "cekirdek_regresyon_saglikli": bool(
            not missing and positive_ok and not false_positive_failures
        ),
    }


def _build(payload: dict) -> dict:
    raw_regions = payload.get("bolgeler") or {}
    region_health = {
        key: _region_health(value)
        for key, value in raw_regions.items()
        if isinstance(value, dict) and value.get("durum") == "ok"
    }
    saturation_regions = sorted(
        key for key, value in region_health.items()
        if value["skor_doygunlugu_riski"]
    )
    regression = _core_regression(raw_regions)

    return {
        "surum": 1,
        "amac": "Seed-merkezli temel/kepçe morfoloji proxy skorunun ayırt ediciliğini ölçmek",
        "uretim_filtresi": False,
        "alarm": False,
        "saha_gorevi": False,
        "rota_degistirir": False,
        "ana_uretim_esigi_degisti": False,
        "mikro_politikasi_degisti": False,
        "doygunluk_esigi": SATURATION_RATIO,
        "minimum_seed": MIN_SEEDS,
        "skor_doygunlugu_var": bool(saturation_regions),
        "doygun_bolgeler": saturation_regions,
        "bagimsiz_kanit_zorunlulugu": True,
        "bolgeler": region_health,
        "cekirdek_regresyon": regression,
        "operasyonel_yorum": (
            "Doygunluk varken morfoloji puanı yalnız diagnostik/ranking girdisidir; "
            "bağımsız SAR, temporal devamlılık veya başka yapılaşma desteği olmadan "
            "yüksek güvenli temel/kepçe adayı sayılmaz."
            if saturation_regions
            else "Morfoloji puanı yine tek başına operasyonel kanıt değildir."
        ),
    }


def _self_check() -> None:
    saturated = _region_health(
        {
            "lokal_seed_sayisi": 100,
            "orta_yuksek_seed_morfoloji": 100,
            "adaylar": [
                {"seed_merkezli_morfoloji_puani": 100},
                {"seed_merkezli_morfoloji_puani": 80},
            ],
        }
    )
    assert saturated["skor_doygunlugu_riski"] is True
    assert saturated["tek_basina_guven_kaniti_olarak_kullanilabilir"] is False

    healthy = _region_health(
        {
            "lokal_seed_sayisi": 100,
            "orta_yuksek_seed_morfoloji": 40,
            "adaylar": [],
        }
    )
    assert healthy["skor_doygunlugu_riski"] is False

    regression = _core_regression(
        {
            "cesme": {
                "saha_referans_regresyonu": [
                    {
                        "id": CORE_POSITIVE_ID,
                        "tespit": True,
                        "regresyon_uyumlu": True,
                    },
                    *[
                        {
                            "id": item_id,
                            "tespit": False,
                            "regresyon_uyumlu": True,
                        }
                        for item_id in sorted(CORE_FALSE_POSITIVE_IDS)
                    ],
                ]
            }
        }
    )
    assert regression["cekirdek_regresyon_saglikli"] is True


def audit() -> dict:
    payload = json.loads(INPUT_JSON.read_text(encoding="utf-8"))
    return _build(payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if args.check_only:
        print("seed morphology discrimination self-check: ok")
        return

    result = audit()
    OUTPUT_JSON.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
