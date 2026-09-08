"""Yakın-ana MİKRO temporal probe kanıtını footprint öncelik çıktısında korur.

Bu katman yalnız diagnostiktir. 150-249 m² bir parça 250 m²+ ana adaya yakın olduğu
için operasyonel MİKRO kısa listeden bastırılmış olsa bile, ayrı temporal probe'da
ölçülen ani başlangıç/devam kanıtı downstream footprint raporunda kaybolmamalıdır.

Bridge sınıflandırma yapmaz ve aday yükseltmez. Yalnız aynı koordinat + alan + Sentinel
kaynak günü eşleşen taze probe kanıtını mevcut arka-plan kaydına ekler. Alarm/saha
görevi false kalır; mikro_footprint_guclu_diagnostik alanına dokunulmaz. Böylece ana
250 m² eşiği korunurken güçlü küçük temporal iz de sessizce takip edilir.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


PRIORITY_FILE = Path(__file__).with_name("micro_site_footprint_priority_review.json")
PROBE_FILE = Path(__file__).with_name("micro_proximity_temporal_probe.json")


def _candidate_key(item):
    try:
        return (
            str(item.get("bolge") or ""),
            round(float(item.get("enlem")), 6),
            round(float(item.get("boylam")), 6),
            round(float(item.get("alan_m2")), 1),
        )
    except (TypeError, ValueError):
        return None


def _probe_index(payload):
    index = {}
    for region in (payload.get("bolgeler") or {}).values():
        if not isinstance(region, dict):
            continue
        latest_date = str(region.get("son_tarih") or "")
        for item in region.get("adaylar") or []:
            if not isinstance(item, dict):
                continue
            key = _candidate_key(item)
            source_date = str(item.get("mikro_kaynak_sentinel_tarihi") or "")
            if key is None or not source_date or source_date != latest_date:
                continue
            if not item.get("temporal_probe_only"):
                continue
            if item.get("operasyonel_kisa_liste") is not False:
                continue
            if item.get("alarm") or item.get("saha_gorevi"):
                continue
            index[key] = item
    return index


def _enrich_row(row, probe_index):
    if not isinstance(row, dict):
        return False
    key = _candidate_key(row)
    probe = probe_index.get(key)
    if probe is None:
        return False

    target_date = str(row.get("mikro_kaynak_sentinel_tarihi") or "")
    probe_date = str(probe.get("mikro_kaynak_sentinel_tarihi") or "")
    if not target_date or target_date != probe_date:
        return False

    # Bu ayrı probe kanıtını footprint temporal kanıtıyla birleştirip aday yükseltme.
    row.update(
        {
            "ana_250m_adaya_yakin": True,
            "yakin_ana_temporal_probe_destegi": bool(
                probe.get("ani_baslangic_destegi")
                or probe.get("devam_eden_hareket_destegi")
            ),
            "yakin_ana_temporal_sinif": probe.get("temporal_sinif"),
            "yakin_ana_temporal_ani_baslangic": bool(probe.get("ani_baslangic_destegi")),
            "yakin_ana_temporal_devam_eden": bool(probe.get("devam_eden_hareket_destegi")),
            "yakin_ana_temporal_ani_baslangic_orani": probe.get("ani_baslangic_orani"),
            "yakin_ana_temporal_gecerli_oran": probe.get("uc_sahne_gecerli_oran"),
            "yakin_ana_250plus_mesafe_m": probe.get("en_yakin_250plus_m"),
            "yakin_ana_temporal_kaynak_tarihi": probe_date,
            "yakin_ana_temporal_notu": (
                "Aynı Sentinel kaynak günündeki 250 m²+ ana adaya yakın MİKRO parçada "
                "temporal probe kanıtı var. Bu kanıt ayrı bir şantiye olduğunu kanıtlamaz; "
                "alarm/görev üretmez ve bağımsız lokal/geometrik ek kanıt bekler."
            ),
        }
    )
    # Güvenlik: bridge operasyonel karar alanlarını asla açmaz.
    row["alarm"] = False
    row["saha_gorevi"] = False
    return True


def bridge(priority_payload, probe_payload):
    probe_index = _probe_index(probe_payload)
    matched_keys = set()

    for collection_name in ("adaylar", "ham_arka_plan_adaylar"):
        for row in priority_payload.get(collection_name) or []:
            if _enrich_row(row, probe_index):
                key = _candidate_key(row)
                if key is not None:
                    matched_keys.add(key)

    priority_payload["yakin_ana_temporal_destekli_arka_plan"] = len(matched_keys)
    priority_payload["yakin_ana_temporal_bridge_alarm_etkisi"] = False
    priority_payload["yakin_ana_temporal_bridge_saha_gorevi_etkisi"] = False
    priority_payload["yakin_ana_temporal_bridge_uyari"] = (
        "Yakın-ana temporal probe yalnız aynı koordinat/alan ve aynı Sentinel kaynak günü "
        "eşleşirse eklenir. Footprint sınıfını yükseltmez; 250 m² ana eşiği ile 150-249 m² "
        "alarm-dışı diagnostik politikası değişmez."
    )
    return priority_payload


def _self_check():
    priority = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": 250,
        "adaylar": [
            {
                "bolge": "cesme",
                "enlem": 38.2,
                "boylam": 26.2,
                "alan_m2": 200,
                "mikro_kaynak_sentinel_tarihi": "05.09.2026",
                "karar_sinifi": "HAM_MIKRO_ARKA_PLAN",
                "mikro_footprint_guclu_diagnostik": False,
                "alarm": False,
                "saha_gorevi": False,
            }
        ],
        "ham_arka_plan_adaylar": [],
    }
    probe = {
        "bolgeler": {
            "cesme": {
                "son_tarih": "05.09.2026",
                "adaylar": [
                    {
                        "bolge": "cesme",
                        "enlem": 38.2,
                        "boylam": 26.2,
                        "alan_m2": 200,
                        "mikro_kaynak_sentinel_tarihi": "05.09.2026",
                        "temporal_probe_only": True,
                        "operasyonel_kisa_liste": False,
                        "alarm": False,
                        "saha_gorevi": False,
                        "ani_baslangic_destegi": True,
                        "devam_eden_hareket_destegi": False,
                        "ani_baslangic_orani": 9.5,
                        "uc_sahne_gecerli_oran": 1.0,
                        "en_yakin_250plus_m": 20,
                        "temporal_sinif": "ANI_BASLANGIC_DESTEGI",
                    }
                ],
            }
        }
    }
    result = bridge(priority, probe)
    row = result["adaylar"][0]
    assert result["ana_uretim_esigi_m2"] == 250
    assert result["yakin_ana_temporal_destekli_arka_plan"] == 1
    assert row["yakin_ana_temporal_probe_destegi"] is True
    assert row["ana_250m_adaya_yakin"] is True
    assert row["karar_sinifi"] == "HAM_MIKRO_ARKA_PLAN"
    assert row["mikro_footprint_guclu_diagnostik"] is False
    assert row["alarm"] is False and row["saha_gorevi"] is False

    # Stale probe aynı koordinatta olsa bile yeni Sentinel gününe taşınmamalı.
    stale_priority = {
        "ana_uretim_esigi_m2": 250,
        "adaylar": [
            {
                "bolge": "cesme",
                "enlem": 38.2,
                "boylam": 26.2,
                "alan_m2": 200,
                "mikro_kaynak_sentinel_tarihi": "10.09.2026",
                "alarm": False,
                "saha_gorevi": False,
            }
        ],
        "ham_arka_plan_adaylar": [],
    }
    stale_result = bridge(stale_priority, probe)
    assert stale_result["yakin_ana_temporal_destekli_arka_plan"] == 0
    assert "yakin_ana_temporal_probe_destegi" not in stale_result["adaylar"][0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Yakın-ana temporal bridge öz testi başarılı; alarm/görev ve 250 m² eşik değişmedi.")
        return

    for path in (PRIORITY_FILE, PROBE_FILE):
        if not path.exists():
            raise RuntimeError(f"{path.name} bulunamadı.")

    priority_payload = json.loads(PRIORITY_FILE.read_text(encoding="utf-8"))
    probe_payload = json.loads(PROBE_FILE.read_text(encoding="utf-8"))
    if priority_payload.get("ana_uretim_esigi_m2", 250) != 250:
        raise RuntimeError("Ana üretim eşiği 250 m² değil; bridge güvenli biçimde durduruldu.")

    result = bridge(priority_payload, probe_payload)
    PRIORITY_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "Yakın-ana temporal kanıt footprint raporuna bağlandı: "
        f"eşleşen-arka-plan={result['yakin_ana_temporal_destekli_arka_plan']}. "
        "Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
