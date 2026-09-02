"""150-249 m² mikro adaylar için kanıt birleştirme / terfi-hazırlık katmanı.

Bu katman alarm veya saha görevi üretmez ve ana 250 m² üretim eşiğini değiştirmez.
Mikro kısa listenin ayrı ayrı üretilmiş spektral, üç-sahne temporal, yerel-kontekst
ve güncel saha geri bildirimi kanıtlarını tek bir diagnostik kararda birleştirir.

Amaç 15 Eylül sonrası için güvenli bir yükseltme kapısı hazırlamaktır: bir mikro aday
ancak güçlü temporal destek + lokal/kompakt karakter + spektral kısa-liste kapısı
birlikte varsa ve geniş-yüzey / güncel saha sonucu / ana 250 m² adaya yakınlık gibi
negatif kanıt taşımıyorsa "güçlü diagnostik" sayılır. Bu etiket yine de otomatik saha
görevi değildir.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import satellite


LOCALITY_FILE = Path(__file__).with_name("micro_site_locality_review.json")
FIELD_FILE = Path(__file__).with_name("micro_site_field_review.json")
OUTPUT_FILE = Path(__file__).with_name("micro_site_decision_review.json")


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _candidate_key(item):
    try:
        latitude = round(float(item.get("enlem")), 6)
        longitude = round(float(item.get("boylam")), 6)
    except (TypeError, ValueError):
        return None
    return (str(item.get("bolge") or ""), latitude, longitude)


def _field_matches(field_payload):
    matches = {}
    for item in field_payload.get("arka_plan_saha_eslesmeleri") or []:
        if not isinstance(item, dict):
            continue
        key = _candidate_key(item)
        if key is None:
            continue
        matches[key] = {
            "guncel_saha_eslesmesi": True,
            "saha_eslesme_sonucu": item.get("saha_eslesme_sonucu"),
            "saha_eslesme_gorev_id": item.get("saha_eslesme_gorev_id"),
            "saha_eslesme_mesafe_m": item.get("saha_eslesme_mesafe_m"),
        }
    return matches


def _classify(item, field_match=None):
    temporal_support = bool(
        item.get("ani_baslangic_destegi")
        or item.get("devam_eden_hareket_destegi")
    )
    local_compact = bool(item.get("lokal_kompakt_destek"))
    broad_risk = bool(
        item.get("genis_yuzey_kontekst_riski")
        or item.get("genis_hareket_kumesi_riski")
    )
    spectral_gate = bool(item.get("spektral_kisa_liste_kapisi", True))
    prior_unstable = bool(item.get("onceki_zemin_hareketli_riski"))
    near_main = bool(item.get("ana_adaya_yakin"))
    field_block = bool(field_match)

    strong = bool(
        spectral_gate
        and temporal_support
        and local_compact
        and not broad_risk
        and not prior_unstable
        and not near_main
        and not field_block
    )

    if field_block:
        outcome = str((field_match or {}).get("saha_eslesme_sonucu") or "")
        if outcome == "SANTIYE_KAZI":
            label = "SAHA_ZATEN_DOGRULANMIS"
            reason = "Yaklaşık aynı noktada güncel saha sonucu zaten şantiye/kazı olarak doğrulanmış."
        else:
            label = "GUNCEL_SAHA_SONUCU_ARKA_PLAN"
            reason = "Yaklaşık aynı noktada aynı/güncel Sentinel sahnesiyle ilişkili saha sonucu var; yeni mikro fırsat gibi yükseltilmedi."
    elif near_main:
        label = "ANA_250M_ADAYA_YAKIN"
        reason = "Mikro sinyal ana 250 m²+ adayın yakınında; ayrı fırsat olarak çoğaltılmadı."
    elif broad_risk:
        label = "GENIS_HAREKET_ARKA_PLAN"
        reason = "Adayın çevresinde yaygın hareket kanıtı var; tarla/toprak temizliği gibi geniş-yüzey değişimi riski nedeniyle yükseltilmedi."
    elif prior_unstable:
        label = "ONCEDEN_HAREKETLI_ZEMIN_ARKA_PLAN"
        reason = "Aynı küçük alanda önceki dönemde de anlamlı hareket var; yeni başlangıç kanıtı yeterince temiz değil."
    elif strong:
        label = "MIKRO_GUCLU_DIAGNOSTIK_KANIT"
        reason = "Spektral kapı + temporal destek + lokal/kompakt karakter birlikte güçlü; 15 Eylül sonrası saha yükseltmesine aday olabilir, fakat otomatik görev değildir."
    elif temporal_support and not local_compact:
        label = "MIKRO_TEMPORAL_BEKLE"
        reason = "Zaman serisi desteği var fakat hareketin bağımsız lokal/kompakt şantiye karakteri henüz yeterince güçlü değil."
    elif local_compact and not temporal_support:
        label = "MIKRO_LOKAL_BEKLE"
        reason = "Hareket çevresine göre lokal/kompakt; ancak yeni başlangıç/devam eden hareket için temporal kanıt henüz yeterli değil."
    else:
        label = "MIKRO_ZAYIF_BEKLE"
        reason = "Mikro sinyal kısa listede kalsa da güçlü temporal ve lokal/kompakt kanıt birlikte oluşmadı."

    return {
        "karar_sinifi": label,
        "karar_nedeni": reason,
        "temporal_destek": temporal_support,
        "lokal_kompakt_destek": local_compact,
        "genis_yuzey_riski": broad_risk,
        "onceki_zemin_hareketli_riski": prior_unstable,
        "ana_250m_adaya_yakin": near_main,
        "guncel_saha_eslesmesi": field_block,
        "mikro_guclu_diagnostik": strong,
        "15_eylul_sonrasi_saha_adayi_olabilir": strong,
    }


def build_review(locality_payload, field_payload):
    field_index = _field_matches(field_payload)
    regions = locality_payload.get("bolgeler") or {}
    rows = []
    for region_key in ("cesme", "uzunkuyu"):
        region = regions.get(region_key) or {}
        for raw in region.get("adaylar") or []:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            match = field_index.get(_candidate_key(item))
            item.update(_classify(item, match))
            if match:
                item.update(match)
            rows.append(item)

    priority = {
        "MIKRO_GUCLU_DIAGNOSTIK_KANIT": 0,
        "MIKRO_TEMPORAL_BEKLE": 1,
        "MIKRO_LOKAL_BEKLE": 2,
        "MIKRO_ZAYIF_BEKLE": 3,
        "ANA_250M_ADAYA_YAKIN": 4,
        "ONCEDEN_HAREKETLI_ZEMIN_ARKA_PLAN": 5,
        "GENIS_HAREKET_ARKA_PLAN": 6,
        "SAHA_ZATEN_DOGRULANMIS": 7,
        "GUNCEL_SAHA_SONUCU_ARKA_PLAN": 8,
    }
    rows.sort(
        key=lambda item: (
            priority.get(str(item.get("karar_sinifi") or ""), 99),
            not bool(item.get("gulbahce_cevre")),
            -_number(item.get("son_donem_skoru"), 0.0),
            -_number(item.get("yerel_kontrast_orani"), 0.0),
        )
    )

    counts = {}
    for item in rows:
        label = str(item.get("karar_sinifi") or "BILINMIYOR")
        counts[label] = counts.get(label, 0) + 1

    strong_rows = [item for item in rows if item.get("mikro_guclu_diagnostik")]
    gulbahce_rows = [item for item in rows if item.get("gulbahce_cevre")]
    gulbahce_strong = [item for item in gulbahce_rows if item.get("mikro_guclu_diagnostik")]

    return {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": locality_payload.get("ana_uretim_esigi_m2", 250),
        "mikro_aralik_m2": locality_payload.get("mikro_aralik_m2", [150, 249]),
        "amac": "Mikro adaylarda spektral + temporal + lokalite + saha geri bildirimi kanıtlarını tek güvenli diagnostik kararda birleştirmek.",
        "terfi_kapisi": {
            "spektral_kisa_liste": True,
            "temporal_destek": True,
            "lokal_kompakt_destek": True,
            "genis_yuzey_riski": False,
            "onceki_zemin_hareketli_riski": False,
            "ana_250m_adaya_yakin": False,
            "guncel_saha_eslesmesi": False,
        },
        "uyari": "MIKRO_GUCLU_DIAGNOSTIK_KANIT bile alarm veya saha görevi değildir. 15 Eylül sonrası operasyonel yükseltme ayrıca ana rota kuralları, yeni Sentinel sahnesi ve saha kapasitesiyle değerlendirilmelidir.",
        "toplam_aday": len(rows),
        "mikro_guclu_diagnostik": len(strong_rows),
        "gulbahce_toplam": len(gulbahce_rows),
        "gulbahce_guclu_diagnostik": len(gulbahce_strong),
        "karar_sayilari": counts,
        "adaylar": rows,
    }


def _self_check():
    assert satellite.MIN_HOTSPOT_AREA_M2 == 250
    base = {
        "bolge": "cesme",
        "enlem": 38.30,
        "boylam": 26.40,
        "spektral_kisa_liste_kapisi": True,
        "ani_baslangic_destegi": True,
        "lokal_kompakt_destek": True,
        "genis_yuzey_kontekst_riski": False,
        "onceki_zemin_hareketli_riski": False,
        "ana_adaya_yakin": False,
    }
    strong = _classify(base)
    assert strong["mikro_guclu_diagnostik"]
    assert strong["karar_sinifi"] == "MIKRO_GUCLU_DIAGNOSTIK_KANIT"

    broad = dict(base)
    broad["genis_yuzey_kontekst_riski"] = True
    broad_result = _classify(broad)
    assert not broad_result["mikro_guclu_diagnostik"]
    assert broad_result["karar_sinifi"] == "GENIS_HAREKET_ARKA_PLAN"

    field_result = _classify(base, {"saha_eslesme_sonucu": "TARLA_BITKI"})
    assert not field_result["mikro_guclu_diagnostik"]
    assert field_result["karar_sinifi"] == "GUNCEL_SAHA_SONUCU_ARKA_PLAN"

    temporal_only = dict(base)
    temporal_only["lokal_kompakt_destek"] = False
    temporal_result = _classify(temporal_only)
    assert temporal_result["karar_sinifi"] == "MIKRO_TEMPORAL_BEKLE"


def run_review():
    _self_check()
    if not LOCALITY_FILE.exists():
        raise RuntimeError("micro_site_locality_review.json bulunamadı.")
    if not FIELD_FILE.exists():
        raise RuntimeError("micro_site_field_review.json bulunamadı.")
    locality_payload = json.loads(LOCALITY_FILE.read_text(encoding="utf-8"))
    field_payload = json.loads(FIELD_FILE.read_text(encoding="utf-8"))
    result = build_review(locality_payload, field_payload)
    OUTPUT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Mikro kanıt birleştirme öz testi başarılı; alarm/görev/eşik değişmedi.")
        return

    result = run_review()
    print(
        "Mikro kanıt birleştirme tamamlandı: "
        f"toplam={result['toplam_aday']}, "
        f"güçlü-diagnostik={result['mikro_guclu_diagnostik']}, "
        f"Gülbahçe={result['gulbahce_toplam']}, "
        f"Gülbahçe-güçlü={result['gulbahce_guclu_diagnostik']}. "
        "Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
