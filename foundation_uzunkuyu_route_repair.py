"""Uzunkuyu ana-eşik temel/kepçe desteğinin rota enjeksiyonunda kaybolmasını onarır.

Pozitif bağlam çıktısında Uzunkuyu grubu tarihsel olarak
``Uzunkuyu · Germiyan · Ildır · Gülbahçe`` etiketi taşır. Ana rota enjektörü bu grup
etiketinde "Gülbahçe" geçtiği için grubun tamamını operasyon dışı sayabiliyordu.
Bu dar onarım yalnız rota enjeksiyonu için grup etiketini çekirdek koridora indirger;
açık Gülbahçe adaylarını ve 26.60 doğusunu yine dışarıda tutar. Eşikler, morfoloji,
SAR skoru ve SQLite değişmez.
"""

from __future__ import annotations

import copy
import json

import foundation_excavation_operational_route_guard as route_guard
import foundation_excavation_supported_route_injector as injector


def _explicit_gulbahce(row):
    if not isinstance(row, dict):
        return False
    text = " ".join(
        str(row.get(key) or "")
        for key in ("mahalle", "yer", "lokasyon", "adres", "bolge")
    ).casefold()
    return "gülbahçe" in text or "gulbahce" in text


def _repair_context(context):
    repaired = copy.deepcopy(context if isinstance(context, dict) else {})
    region = (repaired.get("bolgeler") or {}).get("uzunkuyu")
    if not isinstance(region, dict):
        return repaired

    # Grup etiketi Gülbahçe adını içeriyor diye Uzunkuyu/Germiyan/Ildır adaylarını
    # topluca eleme. Açık Gülbahçe satırları aşağıda ayrıca bastırılır.
    region["bolge"] = "Uzunkuyu · Germiyan · Ildır"
    region["adaylar"] = [
        row
        for row in (region.get("adaylar") or [])
        if isinstance(row, dict) and not _explicit_gulbahce(row)
    ]
    return repaired


def repair(route, context, report, morphology):
    return injector.inject(route, _repair_context(context), report, morphology)


def _self_check():
    base_row = {
        "enlem": 38.262806,
        "boylam": 26.477305,
        "spektral_etki_alani_m2": 300,
        "morfoloji_puani": 90,
        "morfoloji_yuksek": True,
        "bagimsiz_pozitif_destek": True,
        "pozitif_kanit_kaynaklari": ["S2_MORFOLOJI", "S1_LOKAL_POZITIF"],
        "ana_esik_pozitif_destekli_diagnostik_korumali": True,
        "negatif_baglamsal_engel": False,
        "geri_bildirim_engeli": False,
        "mevcut_musteri": False,
        "tarla_bahce_baskilandi": False,
        "yanlis_pozitif_spektral_klon_baskilandi": False,
        "baseline_bitki_negatif_baglami": False,
    }
    context = {
        "bolgeler": {
            "uzunkuyu": {
                "bolge": "Uzunkuyu · Germiyan · Ildır · Gülbahçe",
                "pozitif_s2_son_tarih": "18.09.2026",
                "adaylar": [base_row],
            }
        }
    }
    morphology = {
        "bolgeler": {
            "uzunkuyu": {
                "onceki_tarih": "15.09.2026",
                "son_tarih": "18.09.2026",
            }
        }
    }
    payload = repair({"operasyonel_rota": []}, context, {"saha_adaylari": []}, morphology)
    rows = payload.get("operasyonel_rota") or []
    assert len(rows) == 1, payload
    assert rows[0]["enlem"] == 38.262806, rows
    assert rows[0]["alan_m2"] == 300, rows
    assert "Gülbahçe" not in str(rows[0].get("bolge") or ""), rows

    explicit = copy.deepcopy(context)
    explicit["bolgeler"]["uzunkuyu"]["adaylar"][0]["mahalle"] = "Gülbahçe"
    payload = repair({"operasyonel_rota": []}, explicit, {"saha_adaylari": []}, morphology)
    assert payload.get("operasyonel_rota") == [], payload

    east = copy.deepcopy(context)
    east["bolgeler"]["uzunkuyu"]["adaylar"][0]["boylam"] = 26.64
    payload = repair({"operasyonel_rota": []}, east, {"saha_adaylari": []}, morphology)
    assert payload.get("operasyonel_rota") == [], payload


def main():
    _self_check()
    route = injector._load(injector.ROUTE_JSON)
    report = injector._load(injector.REPORT_JSON)
    context = injector._load(injector.POSITIVE_CONTEXT_JSON)
    morphology = injector._load(injector.MORPHOLOGY_JSON)

    enriched = repair(route, context, report, morphology)
    injector.ROUTE_JSON.write_text(
        json.dumps(enriched, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    updated_report = route_guard._update_report(report, enriched)
    injector.REPORT_JSON.write_text(
        json.dumps(updated_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if injector.FIELD_REPORT_MD.exists():
        text = injector.FIELD_REPORT_MD.read_text(encoding="utf-8")
        injector.FIELD_REPORT_MD.write_text(
            route_guard._update_markdown(text, enriched.get("operasyonel_rota") or []),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
