"""Gülbahçe MİKRO açıklamasını kaynak-gün geniş-yüzey kuralıyla hizalar.

MİKRO kısa liste workflow'u kaynak Sentinel günü farklı olan yakın izleri aynı
250 m geniş-yüzey kümesi saymaz. Gülbahçe açıklama modülü ayrı Python sürecinde
çalıştığı için bu process-local kuralı otomatik devralamazdı ve farklı günlerdeki
yakın izleri yeniden aynı tarla/toprak hareketi gibi işaretleyebilirdi.

Bu köprü yalnız Gülbahçe diagnostik açıklamasını kısa listeyle aynı kaynak-gün
komşuluk hesabına bağlar. 250 m² ana eşik, 150-249 m² MİKRO bant, alarm ve saha
görevi davranışı değişmez.
"""

from __future__ import annotations

import micro_site_cross_day_cluster_guard as cross_day
import micro_site_shortlist as shortlist


# Gülbahçe modülü import edildiğinde aynı shortlist modül nesnesini kullanır.
# Kuralı importtan önce bağlamak, build_review içindeki _annotate çağrısını da
# ana MİKRO kısa listeyle aynı davranışa getirir.
shortlist._annotate = cross_day._annotate_same_source_day

import gulbahce_micro_candidate_review as review  # noqa: E402


def _self_check():
    operation_reference = {
        "enlem": 38.33278,
        "boylam": 26.64556,
        "yaricap_m": 2000,
        "sinir_adres_degil": True,
    }
    strong = {
        "bolge": "uzunkuyu",
        "yaklasik_mevki": "Gülbahçe çevresi",
        "alan_m2": 200,
        "ortalama_rgb_degisim": 0.40,
        "ortalama_ndvi_kaybi": 0.30,
        "ortalama_parlaklik_artisi": 0.20,
    }
    sample = {
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "gulbahce_operasyon_referans": operation_reference,
        "bolgeler": {
            "uzunkuyu": {
                "durum": "ok",
                "son_tarih": "08.09.2026",
                "adaylar": [
                    dict(strong, enlem=38.33278, boylam=26.64556),
                ],
            },
            "eski_sahne_testi": {
                "durum": "ok",
                "son_tarih": "05.09.2026",
                "adaylar": [
                    dict(strong, enlem=38.33320, boylam=26.64556),
                    dict(strong, enlem=38.33278, boylam=26.64610),
                    dict(strong, enlem=38.33230, boylam=26.64556),
                ],
            },
        },
    }

    original_production = shortlist._production_candidates
    try:
        shortlist._production_candidates = lambda: []
        result = review.build_review(sample)
    finally:
        shortlist._production_candidates = original_production

    current = next(
        item
        for item in result["adaylar"]
        if item.get("mikro_kaynak_sentinel_tarihi") == "08.09.2026"
    )
    assert current["250m_mikro_komsu"] == 0, (
        "Gülbahçe açıklaması farklı Sentinel günündeki yakın MİKRO izleri "
        "geniş-yüzey komşusu saymamalıdır."
    )
    assert current["genis_hareket_kumesi_riski"] is False
    assert current["kisa_listeye_girer"] is True
    assert current["alarm"] is False and current["saha_gorevi"] is False


def main():
    cross_day._self_check()
    _self_check()
    review.main()


if __name__ == "__main__":
    main()
