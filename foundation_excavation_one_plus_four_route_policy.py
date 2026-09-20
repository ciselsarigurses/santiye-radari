"""Çekirdek 1+4 saha regresyon seti tamamken gereksiz ikinci pozitif kilidini kaldırır.

Kullanıcının kalibrasyon seti bir doğrulanmış kazı ve dört çekirdek tarla/bahçe
yanlış-pozitifinden oluşur. Önceki rota kapısı en az iki doğrulanmış kazı istediği
için, mevcut set eksiksiz olsa bile yeni 250 m²+ çoklu-kanıt adayların tamamı
kalıcı olarak diagnostikte kalıyordu. Bu katman yalnız çekirdek 1+4 seti eksiksiz
ve zamansal geçerli olduğunda minimum doğrulanmış-kazı sayısını 2'den 1'e indirir.

Morfoloji regresyonu, 250 m² ana eşik, 150–249 m² MİKRO politikası, güçlü
tarla/bahçe negatifleri, dört çekirdek FP klon vetosu ve aynı koordinattaki
Sentinel-1 çapraz destek şartı aynen korunur. Yeni alarm veya görev üretmez;
mevcut rota-kapısı JSON'unu daha önce tanımlı kanıtlarla yeniden değerlendirir.
"""

from __future__ import annotations

import argparse
import json

import foundation_excavation_route_gate_audit as base
import foundation_excavation_core_fp_route_policy as core


DEFAULT_MIN_CONFIRMED = 2
ONE_PLUS_FOUR_MIN_CONFIRMED = 1
REQUIRED_CORE_POSITIVES = 1
REQUIRED_CORE_FALSE_POSITIVES = 4


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _one_plus_four_complete(reference_similarity):
    reference_similarity = reference_similarity if isinstance(reference_similarity, dict) else {}
    return bool(
        reference_similarity.get("cekirdek_regresyon_tam") is True
        and _int(reference_similarity.get("cekirdek_regresyon_pozitif_referans_sayisi"))
        >= REQUIRED_CORE_POSITIVES
        and _int(reference_similarity.get("cekirdek_regresyon_yanlis_pozitif_referans_sayisi"))
        >= REQUIRED_CORE_FALSE_POSITIVES
        and _int(reference_similarity.get("dogrulanmis_kazi_referans_sayisi"))
        >= ONE_PLUS_FOUR_MIN_CONFIRMED
        and _int(reference_similarity.get("zamansal_gecersiz_dogrulanmis_kazi_referans_sayisi")) == 0
    )


def audit(morphology=None, sar=None, feedback=None, reference_similarity=None):
    if reference_similarity is None:
        reference_similarity = base._load(base.REFERENCE_SIMILARITY_JSON)

    one_plus_four_complete = _one_plus_four_complete(reference_similarity)
    original_minimum = base.MIN_CONFIRMED_EXCAVATIONS_FOR_ROUTE
    try:
        base.MIN_CONFIRMED_EXCAVATIONS_FOR_ROUTE = (
            ONE_PLUS_FOUR_MIN_CONFIRMED if one_plus_four_complete else DEFAULT_MIN_CONFIRMED
        )
        payload = core.audit(morphology, sar, feedback, reference_similarity)
    finally:
        base.MIN_CONFIRMED_EXCAVATIONS_FOR_ROUTE = original_minimum

    effective_minimum = ONE_PLUS_FOUR_MIN_CONFIRMED if one_plus_four_complete else DEFAULT_MIN_CONFIRMED
    payload["surum"] = max(_int(payload.get("surum")), 6)
    payload["cekirdek_1arti4_kalibrasyon_tam"] = one_plus_four_complete
    payload["rota_kapisi_icin_min_dogrulanmis_kazi"] = effective_minimum
    payload["rota_kalibrasyon_politikasi"] = (
        "Çekirdek 1 doğrulanmış kazı + 4 kullanıcı-doğrulanmış tarla/bahçe yanlış-pozitifi "
        "eksiksiz ve pozitif referans zamansal geçerliyse ikinci doğrulanmış kazı beklenmez; "
        "aksi durumda eski iki-pozitif emniyet kilidi korunur."
    )
    payload["not"] = (
        "Rota kapısı diagnostiktir; yeni alarm/görev üretmez. Çekirdek 1+4 regresyon seti "
        "tam olduğunda bir zamansal geçerli doğrulanmış kazı kalibrasyon için yeterlidir. "
        "Buna rağmen 250 m² ana eşik, 150–249 m² MİKRO diagnostik sınırı, yüksek morfoloji, "
        "aynı koordinatta zamansal uygun Sentinel-1 çapraz desteği, sıfır morfoloji regresyon "
        "uyumsuzluğu, güçlü tarla/bahçe negatif vetosu ve dört çekirdek FP spektral-klon "
        "vetosu aynen uygulanır. Çekirdek set eksikse minimum iki doğrulanmış kazı kuralına dönülür."
    )
    return payload


def _self_check():
    complete = {
        "dogrulanmis_kazi_referans_sayisi": 1,
        "zamansal_gecersiz_dogrulanmis_kazi_referans_sayisi": 0,
        "cekirdek_regresyon_pozitif_referans_sayisi": 1,
        "cekirdek_regresyon_yanlis_pozitif_referans_sayisi": 4,
        "cekirdek_regresyon_tam": True,
        "bolgeler": {"cesme": {"adaylar": []}, "uzunkuyu": {"adaylar": []}},
    }
    assert _one_plus_four_complete(complete) is True
    incomplete = dict(complete)
    incomplete["cekirdek_regresyon_yanlis_pozitif_referans_sayisi"] = 3
    incomplete["cekirdek_regresyon_tam"] = False
    assert _one_plus_four_complete(incomplete) is False

    morphology = {
        "toplam_regresyon_uyumsuz": 0,
        "bolgeler": {
            "cesme": {
                "bolge": "test",
                "son_tarih": "18.09.2026",
                "adaylar": [{
                    "enlem": 38.30,
                    "boylam": 26.30,
                    "spektral_etki_alani_m2": 400,
                    "seed_merkezli_morfoloji_puani": 90,
                    "seed_merkezli_morfoloji_seviyesi": "YUKSEK",
                    "negatif_kanitlar": [],
                }],
            },
            "uzunkuyu": {"bolge": "test2", "son_tarih": "18.09.2026", "adaylar": []},
        },
    }
    sar = {
        "bolgeler": {
            "cesme": {
                "sar_yeni_tarih": "2026-09-18",
                "sar_optik_doneme_zamansal_uygun": True,
                "tum_sar_sonuclari": [{
                    "enlem": 38.30,
                    "boylam": 26.30,
                    "s2_sar_capraz_destek": True,
                    "sar_lokal_degisim_skor_db": 2.5,
                }],
            },
            "uzunkuyu": {"tum_sar_sonuclari": []},
        }
    }
    feedback_rows = [{
        "id": "TP1",
        "sonuc": "DOGRULANMIS_KAZI",
        "sonuc_tarihi": "2026-09-16",
        "enlem": 38.34,
        "boylam": 26.43,
        "eslesme_yaricapi_m": 25,
    }]
    for index in range(4):
        feedback_rows.append({
            "id": f"FP{index + 1}",
            "sonuc": "YANLIS_POZITIF",
            "sonuc_tarihi": "2026-09-17",
            "enlem": 38.31 + index * 0.001,
            "boylam": 26.50 + index * 0.001,
            "eslesme_yaricapi_m": 25,
        })
    feedback = {"kayitlar": feedback_rows}

    payload = audit(morphology, sar, feedback, complete)
    assert payload["cekirdek_1arti4_kalibrasyon_tam"] is True, payload
    assert payload["rota_kapisi_icin_min_dogrulanmis_kazi"] == 1, payload
    assert payload["toplam_yuksek_guven_diagnostik"] == 1, payload
    assert payload["rota_kapisi_hazir"] is True, payload

    payload = audit(morphology, sar, feedback, incomplete)
    assert payload["cekirdek_1arti4_kalibrasyon_tam"] is False, payload
    assert payload["rota_kapisi_icin_min_dogrulanmis_kazi"] == 2, payload
    assert payload["rota_kapisi_hazir"] is False, payload


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    _self_check()
    if args.check_only:
        print("foundation excavation 1+4 route policy self-check: ok")
        return

    payload = audit()
    base.OUTPUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
