"""Spektral kısa-liste eşiğini çok az kaçıran 150-249 m² mikro adayları ayrıca ölçer.

Ana 250 m² üretim eşiğini ve mevcut MİKRO ŞANTİYE kısa-liste eşiklerini değiştirmez.
Amaç, güçlü/kompakt ham bir aday yalnız tek bir ikinci-aşama spektral metriği dar bir
marjla kaçırdığında onu tamamen görünmez yapmak yerine üç-sahne temporal ve yerel
kontekst testlerinden geçirmektir. Böyle bir aday temporal + lokal olarak güçlü çıksa
bile alarm, saha görevi veya otomatik 15 Eylül terfisi üretmez; yalnız kalibrasyon ve
sonraki Sentinel sahnesi için diagnostik kanıt olarak tutulur.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import satellite
import micro_site_shortlist as shortlist
import micro_site_temporal_guard as temporal
import micro_site_locality_guard as locality


RAW_FILE = Path(__file__).with_name("micro_site_audit.json")
OUTPUT_FILE = Path(__file__).with_name("micro_spectral_borderline_review.json")
ISTANBUL = ZoneInfo("Europe/Istanbul")

RGB_MISS_MARGIN_MAX = 0.03
NDVI_MISS_MARGIN_MAX = 0.02
MIN_FILL_RATIO = 0.75
MAX_REVIEW_CANDIDATES = 6


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _borderline_reason(item):
    """Yalnız tek spektral kapıyı dar marjla kaçıran güvenli adayı kabul et."""
    if not isinstance(item, dict):
        return None
    area = _number(item.get("alan_m2"), 0.0)
    if not (150 <= area < 250):
        return None
    if bool(item.get("genis_hareket_kumesi_riski")):
        return None
    if bool(item.get("ana_adaya_yakin")):
        return None
    if _number(item.get("doluluk_orani"), 0.0) < MIN_FILL_RATIO:
        return None
    if bool(item.get("spektral_kisa_liste_kapisi")):
        return None

    rgb = _number(item.get("ortalama_rgb_degisim"), 0.0)
    ndvi = _number(item.get("ortalama_ndvi_kaybi"), 0.0)
    rgb_ok = rgb >= shortlist.MIN_RGB_CHANGE
    ndvi_ok = ndvi >= shortlist.MIN_NDVI_LOSS

    # İki metriği de kaçıran sinyali kurtarmaya çalışma. Bu katman eşik düşürme yolu değil.
    if rgb_ok == ndvi_ok:
        return None

    if rgb_ok:
        miss = shortlist.MIN_NDVI_LOSS - ndvi
        if 0 < miss <= NDVI_MISS_MARGIN_MAX:
            return {
                "metrik": "NDVI_KAYBI",
                "eksik_marj": round(miss, 4),
                "esik": shortlist.MIN_NDVI_LOSS,
                "deger": round(ndvi, 4),
            }
        return None

    miss = shortlist.MIN_RGB_CHANGE - rgb
    if 0 < miss <= RGB_MISS_MARGIN_MAX:
        return {
            "metrik": "RGB_DEGISIM",
            "eksik_marj": round(miss, 4),
            "esik": shortlist.MIN_RGB_CHANGE,
            "deger": round(rgb, 4),
        }
    return None


def _candidate_pool(raw_payload):
    raw_rows = shortlist._raw_candidates(raw_payload)
    deduped, _ = shortlist._dedupe(raw_rows)
    production = shortlist._production_candidates()
    annotated = shortlist._annotate(deduped, production)

    selected = []
    for row in annotated:
        reason = _borderline_reason(row)
        if reason is None:
            continue
        item = dict(row)
        item["spektral_sinir_inceleme"] = True
        item["spektral_sinir_metrigi"] = reason["metrik"]
        item["spektral_sinir_eksik_marj"] = reason["eksik_marj"]
        item["spektral_sinir_esik"] = reason["esik"]
        item["spektral_sinir_deger"] = reason["deger"]
        item["alarm"] = False
        item["saha_gorevi"] = False
        selected.append(item)

    selected.sort(
        key=lambda item: (
            _number(item.get("spektral_sinir_eksik_marj"), 999.0),
            -shortlist._strength(item),
            -_number(item.get("doluluk_orani"), 0.0),
        )
    )
    return selected[:MAX_REVIEW_CANDIDATES]


def _classify(item):
    temporal_support = bool(
        item.get("ani_baslangic_destegi")
        or item.get("devam_eden_hareket_destegi")
    )
    local_compact = bool(item.get("lokal_kompakt_destek"))
    broad_risk = bool(
        item.get("genis_yuzey_kontekst_riski")
        or item.get("genis_hareket_kumesi_riski")
    )
    prior_unstable = bool(item.get("onceki_zemin_hareketli_riski"))
    reimage = bool(item.get("baglam_yeniden_goruntule"))

    strong = bool(
        temporal_support
        and local_compact
        and not broad_risk
        and not prior_unstable
        and not reimage
    )

    if broad_risk:
        label = "SINIR_GENIS_HAREKET_ARKA_PLAN"
        reason = "Spektral sınır sinyal çevrede yaygın hareket taşıyor; tarla/toprak temizliği riski nedeniyle arka planda tutuldu."
    elif prior_unstable:
        label = "SINIR_ONCEDEN_HAREKETLI_ZEMIN"
        reason = "Aynı alanda önceki dönemde de hareket var; temiz yeni başlangıç kanıtı değil."
    elif reimage:
        label = "SINIR_BAGLAM_YENIDEN_GORUNTULE"
        reason = "Çevre bağlamının güvenilir kısmı yetersiz; negatif hüküm verilmeden sonraki açık Sentinel sahnesine bırakıldı."
    elif strong:
        label = "SINIR_TEMPORAL_LOKAL_GUCLU"
        reason = "Tek spektral eşiği dar marjla kaçırmasına rağmen temporal hareket ve lokal/kompakt karakter birlikte güçlü. Yalnız diagnostik takip; otomatik alarm/görev değildir."
    elif temporal_support:
        label = "SINIR_TEMPORAL_BEKLE"
        reason = "Temporal destek var fakat bağımsız lokal/kompakt karakter henüz yeterli değil."
    elif local_compact:
        label = "SINIR_LOKAL_BEKLE"
        reason = "Lokal/kompakt karakter var fakat ani başlangıç veya devam eden hareket kanıtı yeterli değil."
    else:
        label = "SINIR_ZAYIF_BEKLE"
        reason = "Dar spektral sınır adayında temporal + lokal kanıt birlikte oluşmadı."

    return {
        "sinir_diagnostik_sinif": label,
        "sinir_diagnostik_nedeni": reason,
        "sinir_temporal_destek": temporal_support,
        "sinir_lokal_kompakt_destek": local_compact,
        "sinir_temporal_lokal_guclu": strong,
        "otomatik_alarm": False,
        "otomatik_saha_gorevi": False,
        "15_eylul_otomatik_terfi": False,
    }


def _analyze_region(region_key, rows, metadata):
    if not rows:
        return {
            "durum": "ok",
            "sinir_aday": 0,
            "temporal_lokal_guclu": 0,
            "gulbahce_sinir_aday": 0,
            "gulbahce_temporal_lokal_guclu": 0,
            "adaylar": [],
        }
    if not isinstance(metadata, dict) or metadata.get("durum") != "ok":
        return {
            "durum": "atlandi",
            "neden": "mikro_kaynak_bolge_ok_degil",
            "sinir_aday": len(rows),
            "adaylar": rows,
        }

    temporal_result = temporal._analyze_region(region_key, rows, metadata)
    if temporal_result.get("durum") != "ok":
        return {
            "durum": temporal_result.get("durum", "atlandi"),
            "neden": temporal_result.get("neden", "temporal_analiz_tamamlanamadi"),
            "sinir_aday": len(rows),
            "adaylar": rows,
        }

    locality_result = locality._analyze_region(region_key, temporal_result)
    if locality_result.get("durum") != "ok":
        return {
            "durum": locality_result.get("durum", "atlandi"),
            "neden": locality_result.get("neden", "lokalite_analizi_tamamlanamadi"),
            "sinir_aday": len(rows),
            "adaylar": temporal_result.get("adaylar") or rows,
        }

    analyzed = []
    for raw in locality_result.get("adaylar") or []:
        item = dict(raw)
        item.update(_classify(item))
        analyzed.append(item)

    analyzed.sort(
        key=lambda item: (
            not bool(item.get("sinir_temporal_lokal_guclu")),
            not bool(item.get("gulbahce_cevre")),
            _number(item.get("spektral_sinir_eksik_marj"), 999.0),
            -_number(item.get("yerel_kontrast_orani"), 0.0),
        )
    )
    strong = [item for item in analyzed if item.get("sinir_temporal_lokal_guclu")]
    gulbahce = [item for item in analyzed if item.get("gulbahce_cevre")]
    gulbahce_strong = [
        item for item in gulbahce if item.get("sinir_temporal_lokal_guclu")
    ]
    return {
        "durum": "ok",
        "bolge": satellite.REGIONS[region_key]["label"],
        "sinir_aday": len(analyzed),
        "temporal_lokal_guclu": len(strong),
        "gulbahce_sinir_aday": len(gulbahce),
        "gulbahce_temporal_lokal_guclu": len(gulbahce_strong),
        "adaylar": analyzed,
    }


def build_review(raw_payload):
    rows = _candidate_pool(raw_payload)
    metadata = raw_payload.get("bolgeler") or {}
    regions = {}
    for region_key in ("cesme", "uzunkuyu"):
        region_rows = [row for row in rows if row.get("bolge") == region_key]
        try:
            regions[region_key] = _analyze_region(
                region_key,
                region_rows,
                metadata.get(region_key) or {},
            )
        except Exception as exc:
            regions[region_key] = {
                "durum": "hata",
                "neden": f"{type(exc).__name__}: {exc}",
                "sinir_aday": len(region_rows),
                "adaylar": region_rows,
            }

    ok_regions = [data for data in regions.values() if isinstance(data, dict)]
    return {
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": satellite.MIN_HOTSPOT_AREA_M2,
        "mikro_aralik_m2": raw_payload.get("mikro_aralik_m2", [150, 249]),
        "normal_spektral_esikler": {
            "rgb_min": shortlist.MIN_RGB_CHANGE,
            "ndvi_kaybi_min": shortlist.MIN_NDVI_LOSS,
        },
        "sinir_inceleme_marjlari": {
            "rgb_eksik_maks": RGB_MISS_MARGIN_MAX,
            "ndvi_eksik_maks": NDVI_MISS_MARGIN_MAX,
            "minimum_doluluk": MIN_FILL_RATIO,
            "maksimum_aday": MAX_REVIEW_CANDIDATES,
            "yalniz_tek_spektral_esik_kacabilir": True,
        },
        "amac": "Normal kısa-liste eşiğini değiştirmeden, tek spektral metriği dar marjla kaçıran izole/kompakt mikro adaylarda temporal + lokal kanıtı ölçmek.",
        "uyari": "Bu katman eşik düşürmez. SINIR_TEMPORAL_LOKAL_GUCLU sonucu dahi alarm, saha görevi veya 15 Eylül otomatik terfisi değildir; yalnız kalibrasyon ve sonraki sahne takibidir.",
        "toplam_sinir_aday": sum(int(data.get("sinir_aday") or 0) for data in ok_regions),
        "temporal_lokal_guclu": sum(int(data.get("temporal_lokal_guclu") or 0) for data in ok_regions),
        "gulbahce_sinir_aday": sum(int(data.get("gulbahce_sinir_aday") or 0) for data in ok_regions),
        "gulbahce_temporal_lokal_guclu": sum(int(data.get("gulbahce_temporal_lokal_guclu") or 0) for data in ok_regions),
        "bolgeler": regions,
    }


def _self_check():
    assert satellite.MIN_HOTSPOT_AREA_M2 == 250
    base = {
        "alan_m2": 200,
        "doluluk_orani": 1.0,
        "genis_hareket_kumesi_riski": False,
        "ana_adaya_yakin": False,
        "spektral_kisa_liste_kapisi": False,
        "ortalama_rgb_degisim": 0.3098,
        "ortalama_ndvi_kaybi": 0.2081,
    }
    reason = _borderline_reason(base)
    assert reason and reason["metrik"] == "NDVI_KAYBI"
    assert reason["eksik_marj"] == 0.0119

    both_weak = dict(base)
    both_weak["ortalama_rgb_degisim"] = 0.20
    assert _borderline_reason(both_weak) is None

    full_pass = dict(base)
    full_pass["spektral_kisa_liste_kapisi"] = True
    full_pass["ortalama_ndvi_kaybi"] = 0.23
    assert _borderline_reason(full_pass) is None

    wide = dict(base)
    wide["genis_hareket_kumesi_riski"] = True
    assert _borderline_reason(wide) is None

    strong = dict(base)
    strong.update(
        {
            "ani_baslangic_destegi": True,
            "lokal_kompakt_destek": True,
            "genis_yuzey_kontekst_riski": False,
            "onceki_zemin_hareketli_riski": False,
            "baglam_yeniden_goruntule": False,
        }
    )
    decision = _classify(strong)
    assert decision["sinir_temporal_lokal_guclu"] is True
    assert decision["otomatik_alarm"] is False
    assert decision["otomatik_saha_gorevi"] is False
    assert decision["15_eylul_otomatik_terfi"] is False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Spektral sınır mikro öz testi başarılı; 250 m²/eşik/alarm/görev değişmedi.")
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
        "Spektral sınır mikro incelemesi tamamlandı: "
        f"aday={result['toplam_sinir_aday']}, "
        f"temporal+lokal güçlü={result['temporal_lokal_guclu']}, "
        f"Gülbahçe={result['gulbahce_sinir_aday']}, "
        f"Gülbahçe güçlü={result['gulbahce_temporal_lokal_guclu']}. "
        "Alarm/görev/otomatik terfi üretilmedi."
    )


if __name__ == "__main__":
    main()
