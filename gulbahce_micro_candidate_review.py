"""Gülbahçe ham MİKRO ŞANTİYE adaylarının kısa-listeden elenme nedenini açıklar.

Bu dosya seçim eşiklerini değiştirmez. Ham 150-249 m² adayları mevcut kısa-liste
kurallarıyla aynı biçimde tekilleştirip işaretler; Gülbahçe çevresindeki adayın geniş
hareket, 250 m²+ ana adaya yakınlık veya spektral kapı nedeniyle elenip elenmediğini
kalibrasyon için kalıcı bir çıktıda gösterir. Alarm veya saha görevi üretmez.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import micro_site_shortlist as shortlist


RAW_FILE = Path(__file__).with_name("micro_site_audit.json")
OUTPUT_FILE = Path(__file__).with_name("gulbahce_micro_candidate_review.json")


def _is_gulbahce(item):
    return str(item.get("yaklasik_mevki") or "").startswith("Gülbahçe")


def _review_item(item):
    row = dict(item)
    reasons = []
    if row.get("genis_hareket_kumesi_riski"):
        reasons.append("GENIS_HAREKET_KUMESI")
    if row.get("ana_adaya_yakin"):
        reasons.append("ANA_250PLUS_ADAYA_YAKIN")
    if not row.get("spektral_kisa_liste_kapisi"):
        reasons.append("SPEKTRAL_KISA_LISTE_KAPISI")

    rgb = shortlist._number(row.get("ortalama_rgb_degisim"), 0.0)
    ndvi = shortlist._number(row.get("ortalama_ndvi_kaybi"), 0.0)
    row["kisa_listeye_girer"] = not reasons
    row["elenme_nedenleri"] = reasons
    row["spektral_esikler"] = {
        "rgb_min": shortlist.MIN_RGB_CHANGE,
        "ndvi_kaybi_min": shortlist.MIN_NDVI_LOSS,
        "rgb_gecer": rgb >= shortlist.MIN_RGB_CHANGE,
        "ndvi_gecer": ndvi >= shortlist.MIN_NDVI_LOSS,
        "rgb_marj": round(rgb - shortlist.MIN_RGB_CHANGE, 4),
        "ndvi_marj": round(ndvi - shortlist.MIN_NDVI_LOSS, 4),
    }
    row["yalniz_sinirda_spektral_nedenle_elendi"] = bool(
        reasons == ["SPEKTRAL_KISA_LISTE_KAPISI"]
    )
    row["alarm"] = False
    row["saha_gorevi"] = False
    return row


def build_review(raw_payload):
    raw_rows = shortlist._raw_candidates(raw_payload)
    deduped, _ = shortlist._dedupe(raw_rows)
    production = shortlist._production_candidates()
    annotated = shortlist._annotate(deduped, production)
    gulbahce = [_review_item(item) for item in annotated if _is_gulbahce(item)]

    return {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": raw_payload.get("ana_uretim_esigi_m2", 250),
        "mikro_aralik_m2": raw_payload.get("mikro_aralik_m2", [150, 249]),
        "kaynak_olusturma": raw_payload.get("olusturma"),
        "gulbahce_ham_tekil_aday": len(gulbahce),
        "gulbahce_kisa_listeye_giren": sum(bool(item["kisa_listeye_girer"]) for item in gulbahce),
        "yalniz_sinirda_spektral_nedenle_elenen": sum(
            bool(item["yalniz_sinirda_spektral_nedenle_elendi"]) for item in gulbahce
        ),
        "adaylar": gulbahce,
        "yorum": (
            "Bu çıktı yalnız kalibrasyon açıklamasıdır. Ham mikro adayın neden kısa-listede "
            "olmadığını görünür kılar; hiçbir eşiği düşürmez, alarm veya saha görevi üretmez."
        ),
    }


def _self_check():
    sample = {
        "bolgeler": {
            "uzunkuyu": {
                "adaylar": [
                    {
                        "bolge": "uzunkuyu",
                        "yaklasik_mevki": "Gülbahçe çevresi",
                        "enlem": 38.33,
                        "boylam": 26.646,
                        "alan_m2": 200,
                        "ortalama_rgb_degisim": 0.31,
                        "ortalama_ndvi_kaybi": 0.208,
                        "ortalama_parlaklik_artisi": 0.31,
                    }
                ]
            }
        }
    }
    original = shortlist._production_candidates
    try:
        shortlist._production_candidates = lambda: []
        result = build_review(sample)
    finally:
        shortlist._production_candidates = original
    assert result["gulbahce_ham_tekil_aday"] == 1
    item = result["adaylar"][0]
    assert item["spektral_esikler"]["rgb_gecer"] is True
    assert item["spektral_esikler"]["ndvi_gecer"] is False
    assert item["yalniz_sinirda_spektral_nedenle_elendi"] is True
    assert item["alarm"] is False and item["saha_gorevi"] is False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Gülbahçe mikro aday açıklama öz testi başarılı; eşikler değişmedi.")
        return

    if not RAW_FILE.exists():
        raise RuntimeError("micro_site_audit.json bulunamadı.")
    raw_payload = json.loads(RAW_FILE.read_text(encoding="utf-8"))
    result = build_review(raw_payload)
    OUTPUT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Gülbahçe mikro aday açıklaması: "
        f"ham={result['gulbahce_ham_tekil_aday']}, "
        f"kısa-liste={result['gulbahce_kisa_listeye_giren']}, "
        f"yalnız-spektral={result['yalniz_sinirda_spektral_nedenle_elenen']}. "
        "Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
