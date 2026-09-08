"""250 m²+ ana adaya yakın olduğu için kısa listeden bastırılan güçlü MİKRO sinyalleri temporalde izle.

Bu yan katman yalnız diagnostiktir. Ana 250 m² alarm eşiğini değiştirmez, 150-249 m²
adayları saha görevine veya operasyonel kısa listeye taşımaz. Amaç aynı günkü 250 m²+
üretim adayına 150 m içinde olduğu için mevcut MİKRO kısa listesinden çıkarılan bir
kompakt/spektral güçlü parçanın temporal ani başlangıç veya devam eden hareket
kanıtını tamamen kaybetmemektir.

Yakınlık tek başına ayrı bir şantiye kanıtı değildir: temporal destek bulunsa bile bu
çıktı ana adayın aynı fiziksel olayının parçası olabilir. Ayrı fırsat sayılması için
sonraki katmanlarda bağımsız lokal/geometrik veya güvenilir ek yapılaşma kanıtı gerekir.
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


RAW_FILE = Path(__file__).with_name("micro_site_audit.json")
OUTPUT_FILE = Path(__file__).with_name("micro_proximity_temporal_probe.json")
ISTANBUL = ZoneInfo("Europe/Istanbul")
PROBE_LIMIT = 8


def _select_probe_rows(annotated):
    """Yalnız yakınlık yüzünden operasyonel MİKRO kısa listesinden düşen güçlü parçaları seç."""
    rows = []
    for raw in annotated:
        if not isinstance(raw, dict):
            continue
        if raw.get("genis_hareket_kumesi_riski"):
            continue
        if not raw.get("ana_adaya_yakin"):
            continue
        if not raw.get("spektral_kisa_liste_kapisi"):
            continue
        item = dict(raw)
        item["temporal_probe_only"] = True
        item["operasyonel_kisa_liste"] = False
        item["probe_nedeni"] = (
            "Aynı kaynak Sentinel günündeki 250 m²+ ana adaya yakın olduğu için normal "
            "MİKRO kısa listeden bastırıldı; güçlü spektral/kompakt iz kaybolmasın diye "
            "yalnız temporal diagnostikte izleniyor."
        )
        rows.append(item)

    rows.sort(
        key=lambda item: (
            shortlist._number(item.get("en_yakin_250plus_m"), 10_000),
            -shortlist._strength(item),
            shortlist._number(item.get("alan_m2"), 9999),
        )
    )
    return rows[:PROBE_LIMIT]


def _raw_region_metadata(raw_payload):
    return {
        key: value
        for key, value in (raw_payload.get("bolgeler") or {}).items()
        if isinstance(value, dict)
    }


def _self_check():
    assert satellite.MIN_HOTSPOT_AREA_M2 == 250

    base = {
        "bolge": "cesme",
        "enlem": 38.3,
        "boylam": 26.3,
        "alan_m2": 200,
        "ortalama_rgb_degisim": 0.35,
        "ortalama_ndvi_kaybi": 0.28,
        "ortalama_parlaklik_artisi": 0.20,
        "genis_hareket_kumesi_riski": False,
        "ana_adaya_yakin": True,
        "spektral_kisa_liste_kapisi": True,
        "en_yakin_250plus_m": 80,
    }
    selected = _select_probe_rows([base])
    assert len(selected) == 1
    assert selected[0]["temporal_probe_only"] is True
    assert selected[0]["operasyonel_kisa_liste"] is False

    not_near = dict(base, ana_adaya_yakin=False)
    assert _select_probe_rows([not_near]) == []

    broad = dict(base, genis_hareket_kumesi_riski=True)
    assert _select_probe_rows([broad]) == []

    weak = dict(base, spektral_kisa_liste_kapisi=False)
    assert _select_probe_rows([weak]) == []


def run_probe():
    _self_check()
    if not RAW_FILE.exists():
        raise RuntimeError("micro_site_audit.json bulunamadı.")

    raw_payload = json.loads(RAW_FILE.read_text(encoding="utf-8"))
    raw_rows = shortlist._raw_candidates(raw_payload)
    deduped, _ = shortlist._dedupe(raw_rows)
    source_dates = shortlist._current_source_dates(raw_payload)
    production_all = shortlist._production_candidates()
    production = shortlist._filter_current_production(production_all, source_dates)
    annotated = shortlist._annotate(deduped, production)
    rows = _select_probe_rows(annotated)
    metadata = _raw_region_metadata(raw_payload)

    payload = {
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": satellite.MIN_HOTSPOT_AREA_M2,
        "mikro_aralik_m2": raw_payload.get("mikro_aralik_m2", [150, 249]),
        "operasyonel_kisa_listeye_etki": False,
        "ana_adaya_yakinlik_yaricapi_m": shortlist.PRODUCTION_NEAR_RADIUS_M,
        "ham_mikro": len(raw_rows),
        "tekil_mikro": len(deduped),
        "guncel_250plus_referans": len(production),
        "temporal_probe_girdi": len(rows),
        "amac": (
            "Ana 250 m²+ adaya yakınlık nedeniyle kısa listeden bastırılan fakat geniş-yüzey "
            "kümesi olmayan ve spektral kapıyı geçen 150-249 m² parçaların temporal kanıtını "
            "kaybetmeden, yalnız diagnostik olarak izlemek."
        ),
        "uyari": (
            "Temporal destek, bu parçanın ana 250 m²+ adaydan bağımsız ikinci bir şantiye "
            "olduğunu kanıtlamaz. Alarm/görev üretmez; ayrı fırsat için bağımsız lokal/geometrik "
            "veya güvenilir ek yapılaşma kanıtı gerekir."
        ),
        "bolgeler": {},
    }

    for region_key in ("cesme", "uzunkuyu"):
        region_rows = [row for row in rows if row.get("bolge") == region_key]
        if not region_rows:
            payload["bolgeler"][region_key] = {
                "durum": "ok",
                "olculen_aday": 0,
                "ani_baslangic_destegi": 0,
                "devam_eden_hareket_destegi": 0,
                "gulbahce_olculen": 0,
                "adaylar": [],
            }
            continue

        region_metadata = metadata.get(region_key) or {}
        if region_metadata.get("durum") != "ok":
            payload["bolgeler"][region_key] = {
                "durum": "atlandi",
                "neden": "mikro_kaynak_bolge_ok_degil",
                "aday_sayisi": len(region_rows),
            }
            continue

        try:
            result = temporal._analyze_region(region_key, region_rows, region_metadata)
            for item in result.get("adaylar") or []:
                item["temporal_probe_only"] = True
                item["operasyonel_kisa_liste"] = False
            payload["bolgeler"][region_key] = result
        except Exception as exc:
            payload["bolgeler"][region_key] = {
                "durum": "hata",
                "neden": f"{type(exc).__name__}: {exc}",
                "aday_sayisi": len(region_rows),
            }

    payload["toplam"] = {
        key: sum(
            int(data.get(key) or 0)
            for data in payload["bolgeler"].values()
            if isinstance(data, dict)
        )
        for key in (
            "olculen_aday",
            "ani_baslangic_destegi",
            "devam_eden_hareket_destegi",
            "onceki_zemin_hareketli_riski",
            "gulbahce_olculen",
        )
    }

    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Yakın-ana MİKRO temporal probe öz testi başarılı; alarm/görev/eşik değişmedi.")
        return

    payload = run_probe()
    total = payload.get("toplam") or {}
    print(
        "Yakın-ana MİKRO temporal probe tamamlandı: "
        f"girdi={payload['temporal_probe_girdi']}, "
        f"ölçülen={int(total.get('olculen_aday') or 0)}, "
        f"ani={int(total.get('ani_baslangic_destegi') or 0)}, "
        f"devam={int(total.get('devam_eden_hareket_destegi') or 0)}, "
        f"Gülbahçe={int(total.get('gulbahce_olculen') or 0)}. "
        "Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
