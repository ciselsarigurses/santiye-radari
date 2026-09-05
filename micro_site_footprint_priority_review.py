"""Bileşen-izi temporal audit ile lokal mikro kanıtı güvenli biçimde birleştirir.

Bu katman yalnız diagnostiktir. Ana Sentinel saha alarmı 250 m² olarak kalır;
150-249 m² adaylardan alarm veya saha görevi üretmez. Sabit 3x3 pencerenin gerçek
iki-piksel mikro sinyali seyreltmiş olabildiği durumlarda, yalnız adayın kendi bağlı
bileşeninde güçlü temporal destek VE bağımsız lokal/kompakt bağlam birlikte varsa
"footprint güçlü diagnostik" etiketi üretir.

Geniş-yüzey riski, önceki zeminin zaten hareketli olması, ana 250 m²+ adaya yakınlık
ve güncel saha eşleşmesi yükseltmeyi engeller. Yetersiz geçerli 9x9 çevre bağlamı ise
"lokal değil" diye yorumlanmaz; güçlü temporal sinyal arka planda sonraki açık Sentinel
sahnesinde yeniden bağlam ölçümü bekler. Böylece eşiği düşürmek yerine ölçülen lokal +
temporal kanıt birlikte aranır.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import satellite
import micro_site_locality_guard as locality_guard


FOOTPRINT_FILE = Path(__file__).with_name("micro_site_temporal_footprint_audit.json")
LOCALITY_FILE = Path(__file__).with_name("micro_site_locality_review.json")
FIELD_FILE = Path(__file__).with_name("micro_site_field_review.json")
OUTPUT_FILE = Path(__file__).with_name("micro_site_footprint_priority_review.json")


def _candidate_key(item):
    try:
        latitude = round(float(item.get("enlem")), 6)
        longitude = round(float(item.get("boylam")), 6)
    except (TypeError, ValueError):
        return None
    return (str(item.get("bolge") or ""), latitude, longitude)


def _flatten_regions(payload):
    rows = []
    for region in (payload.get("bolgeler") or {}).values():
        if not isinstance(region, dict):
            continue
        rows.extend(
            dict(item)
            for item in (region.get("adaylar") or [])
            if isinstance(item, dict)
        )
    return rows


def _field_index(payload):
    index = {}
    for item in payload.get("inceleme_adaylari") or []:
        if not isinstance(item, dict):
            continue
        key = _candidate_key(item)
        if key is not None:
            index[key] = item
    return index


def _context_fraction(locality):
    raw = locality.get("baglam_gecerli_oran")
    if raw is None:
        # Eski/eksik diagnostik çıktıda yeni bir negatif kanıt uydurma.
        return 1.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 1.0


def _classify(footprint, locality, field):
    component_temporal = bool(
        footprint.get("bilesen_ani_baslangic")
        or footprint.get("bilesen_devam_eden")
    )
    exact_component = bool(
        footprint.get("bilesen_eslesti")
        and float(footprint.get("bilesen_esleme_piksel_mesafesi") or 0.0) == 0.0
    )
    component_valid = float(footprint.get("bilesen_gecerli_oran") or 0.0) >= (2 / 3)
    spectral_gate = bool(footprint.get("spektral_kisa_liste_kapisi", True))
    local_compact = bool(locality.get("lokal_kompakt_destek"))
    context_fraction = _context_fraction(locality)
    context_sufficient = context_fraction >= locality_guard.MIN_CONTEXT_VALID_FRACTION
    broad_risk = bool(
        locality.get("genis_yuzey_kontekst_riski")
        or footprint.get("genis_hareket_kumesi_riski")
    )
    prior_unstable = bool(footprint.get("bilesen_onceki_zemin_hareketli_riski"))
    near_main = bool(footprint.get("ana_adaya_yakin"))
    field_match = bool((field or {}).get("guncel_saha_eslesmesi"))

    strong = bool(
        exact_component
        and component_valid
        and spectral_gate
        and component_temporal
        and local_compact
        and context_sufficient
        and not broad_risk
        and not prior_unstable
        and not near_main
        and not field_match
    )

    if strong:
        label = "MIKRO_FOOTPRINT_GUCLU_DIAGNOSTIK"
        reason = (
            "Adayın kendi bağlı bileşeninde temporal hareket + bağımsız lokal/kompakt "
            "karakter birlikte güçlü. Alarm/görev değildir; 15 Eylül sonrası yeni "
            "Sentinel sahnesinde öncelikli tekrar doğrulama adayıdır."
        )
    elif broad_risk:
        label = "GENIS_HAREKET_ARKA_PLAN"
        reason = "Çevresel yaygın hareket riski var; mikro bileşen temporal sinyal verse de yükseltilmedi."
    elif component_temporal and not context_sufficient:
        label = "FOOTPRINT_TEMPORAL_VAR_BAGLAM_YETERSIZ"
        reason = (
            "Bileşenin temporal desteği var fakat 9x9 çevre bağlamının geçerli Sentinel "
            "oranı lokal/kompakt yorumu için yetersiz. Aday silinmez veya 'lokal değil' "
            "sayılmaz; sonraki açık sahnede çevre bağlamı yeniden ölçülür."
        )
    elif component_temporal and not local_compact:
        label = "FOOTPRINT_TEMPORAL_VAR_LOKAL_DEGIL"
        reason = "Bileşenin temporal desteği var fakat yeterli geçerli çevre bağlamında bağımsız lokal/kompakt karakter yok."
    elif local_compact and not component_temporal:
        label = "LOKAL_KOMPAKT_VAR_TEMPORAL_YETERSIZ"
        reason = "Lokal/kompakt karakter var fakat aday bileşeninde temporal destek yeterli değil."
    else:
        label = "BEKLE"
        reason = "Güçlü footprint temporal + lokal/kompakt kanıt birlikte oluşmadı."

    return {
        "karar_sinifi": label,
        "karar_nedeni": reason,
        "footprint_temporal_destek": component_temporal,
        "footprint_tam_esleme": exact_component,
        "footprint_gecerli": component_valid,
        "baglam_gecerli": context_sufficient,
        "baglam_gecerli_oran": round(context_fraction, 3),
        "baglam_yeniden_goruntuleme_onceligi": bool(
            component_temporal and not context_sufficient
        ),
        "lokal_kompakt_destek": local_compact,
        "genis_yuzey_riski": broad_risk,
        "onceki_zemin_hareketli_riski": prior_unstable,
        "ana_250m_adaya_yakin": near_main,
        "guncel_saha_eslesmesi": field_match,
        "mikro_footprint_guclu_diagnostik": strong,
        "alarm": False,
        "saha_gorevi": False,
    }


def build_review(footprint_payload, locality_payload, field_payload):
    footprint_rows = _flatten_regions(footprint_payload)
    locality_index = {
        _candidate_key(item): item
        for item in _flatten_regions(locality_payload)
        if _candidate_key(item) is not None
    }
    field_index = _field_index(field_payload)

    rows = []
    for raw in footprint_rows:
        key = _candidate_key(raw)
        locality = locality_index.get(key) or {}
        field = field_index.get(key) or {}
        item = dict(raw)
        classification = _classify(item, locality, field)
        item.update(classification)
        if locality:
            item["yerel_kontrast_orani"] = locality.get("yerel_kontrast_orani")
            if classification.get("baglam_gecerli") is False:
                item["lokalite_sinifi"] = "YETERSIZ_BAGLAM_VERISI"
            else:
                item["lokalite_sinifi"] = locality.get("lokalite_sinifi")
        rows.append(item)

    priority = {
        "MIKRO_FOOTPRINT_GUCLU_DIAGNOSTIK": 0,
        "LOKAL_KOMPAKT_VAR_TEMPORAL_YETERSIZ": 1,
        "FOOTPRINT_TEMPORAL_VAR_BAGLAM_YETERSIZ": 2,
        "FOOTPRINT_TEMPORAL_VAR_LOKAL_DEGIL": 3,
        "BEKLE": 4,
        "GENIS_HAREKET_ARKA_PLAN": 5,
    }
    rows.sort(
        key=lambda item: (
            priority.get(str(item.get("karar_sinifi") or ""), 99),
            -float(item.get("bilesen_son_skor") or 0.0),
        )
    )

    counts = {}
    for item in rows:
        label = str(item.get("karar_sinifi") or "BILINMIYOR")
        counts[label] = counts.get(label, 0) + 1

    strong = [row for row in rows if row.get("mikro_footprint_guclu_diagnostik")]
    context_wait = [
        row for row in rows if row.get("baglam_yeniden_goruntuleme_onceligi")
    ]
    gulbahce = [
        row for row in rows
        if str(row.get("yaklasik_mevki") or "").startswith("Gülbahçe")
        or bool(row.get("gulbahce_cevre"))
    ]

    return {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": footprint_payload.get("ana_uretim_esigi_m2", 250),
        "mikro_aralik_m2": footprint_payload.get("mikro_aralik_m2", [150, 249]),
        "amac": "Sabit 3x3 pencerenin seyreltme riskine karşı gerçek mikro bileşen temporal kanıtını lokal/kompakt bağlamla birlikte değerlendirmek.",
        "uyari": "Güçlü footprint diagnostik etiketi alarm veya saha görevi değildir; yeni Sentinel sahnesinde tekrar doğrulama ve saha kalibrasyonu beklenir. Geçerli çevre bağlamı yetersiz temporal adaylar negatif kabul edilmez, ayrı yeniden-görüntüleme bekleme sınıfında korunur.",
        "toplam_aday": len(rows),
        "guclu_footprint_diagnostik": len(strong),
        "baglam_yeniden_goruntuleme": len(context_wait),
        "gulbahce_toplam": len(gulbahce),
        "gulbahce_guclu": sum(bool(row.get("mikro_footprint_guclu_diagnostik")) for row in gulbahce),
        "karar_sayilari": counts,
        "guclu_adaylar": strong,
        "baglam_bekleyen_adaylar": context_wait,
        "adaylar": rows,
    }


def _self_check():
    assert satellite.MIN_HOTSPOT_AREA_M2 == 250
    footprint = {
        "spektral_kisa_liste_kapisi": True,
        "bilesen_eslesti": True,
        "bilesen_esleme_piksel_mesafesi": 0.0,
        "bilesen_gecerli_oran": 1.0,
        "bilesen_ani_baslangic": True,
        "bilesen_devam_eden": False,
        "bilesen_onceki_zemin_hareketli_riski": False,
        "genis_hareket_kumesi_riski": False,
        "ana_adaya_yakin": False,
    }
    locality = {
        "lokal_kompakt_destek": True,
        "genis_yuzey_kontekst_riski": False,
        "baglam_gecerli_oran": 1.0,
    }
    result = _classify(footprint, locality, {})
    assert result["mikro_footprint_guclu_diagnostik"]
    assert result["karar_sinifi"] == "MIKRO_FOOTPRINT_GUCLU_DIAGNOSTIK"

    broad = dict(locality)
    broad["genis_yuzey_kontekst_riski"] = True
    result = _classify(footprint, broad, {})
    assert not result["mikro_footprint_guclu_diagnostik"]
    assert result["karar_sinifi"] == "GENIS_HAREKET_ARKA_PLAN"

    blind_context = {
        "lokal_kompakt_destek": False,
        "genis_yuzey_kontekst_riski": False,
        "baglam_gecerli_oran": 0.389,
    }
    result = _classify(footprint, blind_context, {})
    assert not result["mikro_footprint_guclu_diagnostik"]
    assert result["karar_sinifi"] == "FOOTPRINT_TEMPORAL_VAR_BAGLAM_YETERSIZ"
    assert result["baglam_yeniden_goruntuleme_onceligi"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Footprint mikro öncelik öz testi başarılı; 250 m² eşik/alarm/görev değişmedi.")
        return

    for path in (FOOTPRINT_FILE, LOCALITY_FILE, FIELD_FILE):
        if not path.exists():
            raise RuntimeError(f"{path.name} bulunamadı.")
    footprint_payload = json.loads(FOOTPRINT_FILE.read_text(encoding="utf-8"))
    locality_payload = json.loads(LOCALITY_FILE.read_text(encoding="utf-8"))
    field_payload = json.loads(FIELD_FILE.read_text(encoding="utf-8"))
    result = build_review(footprint_payload, locality_payload, field_payload)
    OUTPUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "Footprint mikro öncelik incelemesi: "
        f"toplam={result['toplam_aday']}, güçlü={result['guclu_footprint_diagnostik']}, "
        f"bağlam-bekleyen={result['baglam_yeniden_goruntuleme']}, "
        f"Gülbahçe={result['gulbahce_toplam']}, Gülbahçe-güçlü={result['gulbahce_guclu']}. "
        "Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
