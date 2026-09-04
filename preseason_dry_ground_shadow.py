"""15 Eylül öncesi kuru-zemin teyit geçidini gölge modunda kalibre eder.

Bu katman üretim alarmına, saha görevine veya günlük rotaya dokunmaz. Amaç, 15 Eylül
sonrası kullanılacak ``postseason_dry_ground_confirmation_guard`` kurallarını mevcut
Sentinel sahnelerinde önceden çalıştırıp kaç adayın gerçekten bütün kanıtları birlikte
taşıdığını ölçmektir.

Ana Sentinel alarm alt eşiği 250 m² olarak kalır. Gölge aday ancak mevcut 250-900 m²
kuru-zemin diagnostik havuzunda uzun-temporal ani başlangıç + kararlı geçmiş zemin +
yörünge güveni + izole/non-lineer geometri + 5x5 çevreye göre lokal değişim kanıtını
birlikte taşıyorsa tutulur. Ana saha kuyruğunda 40 m içinde zaten görev varsa ayrıca
gölge aday sayılmaz.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import postseason_dry_ground_confirmation_guard as gate


BASE = Path(__file__).resolve().parent
REPORT_JSON = BASE / "latest_report.json"
TEMPORAL_AUDIT = BASE / "dry_ground_temporal_audit.json"
LOCALITY_AUDIT = BASE / "temporal_locality_audit.json"
OUTPUT_JSON = BASE / "preseason_dry_ground_shadow.json"


def _load(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _shadow_candidate(temporal_item, locality_item, region_label, scene_item):
    lat = round(gate._number(temporal_item.get("enlem")), 6)
    lon = round(gate._number(temporal_item.get("boylam")), 6)
    return {
        "mahalle": str(temporal_item.get("mahalle") or "Mevki doğrulanmadı"),
        "enlem": lat,
        "boylam": lon,
        "alan_m2": round(gate._number(temporal_item.get("alan_m2"))),
        "bolge": gate._canonical_region(region_label),
        "son_item": str(scene_item or ""),
        "yerellik_orani": round(gate._number(locality_item.get("yerellik_orani")), 3),
        "uzun_temporal_ani_baslangic_orani": round(
            gate._number(temporal_item.get("uzun_temporal_ani_baslangic_orani")), 3
        ),
        "son_cift_bsi_degisim": round(gate._number(temporal_item.get("son_cift_bsi_degisim")), 4),
        "alarm": False,
        "saha_gorevi": False,
        "gölge_kalibrasyon": True,
        "harita": f"https://www.google.com/maps/dir/?api=1&destination={lat:.6f},{lon:.6f}",
    }


def build_shadow(report, temporal_payload, locality_payload):
    if not all(isinstance(v, dict) for v in (report, temporal_payload, locality_payload)):
        return None

    report_date = str(report.get("rapor_tarihi") or "")
    if not report_date:
        return None
    if str(temporal_payload.get("rapor_tarihi") or "") != report_date:
        return None
    if str(locality_payload.get("rapor_tarihi") or "") != report_date:
        return None

    temporal_regions = temporal_payload.get("bolgeler") or {}
    locality_regions = locality_payload.get("bolgeler") or {}
    if not isinstance(temporal_regions, dict) or not isinstance(locality_regions, dict):
        return None

    candidates = []
    region_summary = {}
    for region_key, temporal_region in temporal_regions.items():
        locality_region = locality_regions.get(region_key)
        if not isinstance(temporal_region, dict) or not isinstance(locality_region, dict):
            continue
        if temporal_region.get("durum") != "ok" or locality_region.get("durum") != "ok":
            continue

        scene_item = str(temporal_region.get("son_item") or "")
        if not scene_item or scene_item != str(locality_region.get("son_item") or ""):
            continue

        temporal_by_point = gate._temporal_map(temporal_region)
        strong = []
        duplicates = 0
        for locality_item in locality_region.get("adaylar") or []:
            if not isinstance(locality_item, dict):
                continue
            temporal_item = temporal_by_point.get(gate._point_key(locality_item))
            if not isinstance(temporal_item, dict):
                continue
            if not gate._is_strong_pair(temporal_item, locality_item):
                continue

            candidate = _shadow_candidate(
                temporal_item,
                locality_item,
                temporal_region.get("bolge"),
                scene_item,
            )
            if gate._near_existing_task(candidate, report):
                duplicates += 1
                continue
            strong.append(candidate)

        strong.sort(
            key=lambda item: (
                -gate._number(item.get("yerellik_orani")),
                -gate._number(item.get("uzun_temporal_ani_baslangic_orani")),
                -gate._number(item.get("son_cift_bsi_degisim")),
                gate._number(item.get("alan_m2")),
            )
        )
        candidates.extend(strong)
        region_summary[region_key] = {
            "bolge": gate._canonical_region(temporal_region.get("bolge")),
            "son_item": scene_item,
            "son_tarih": temporal_region.get("son_tarih"),
            "guclu_golge_aday": len(strong),
            "ana_goreve_yakin_oldugu_icin_atlanan": duplicates,
        }

    candidates.sort(
        key=lambda item: (
            -gate._number(item.get("yerellik_orani")),
            -gate._number(item.get("uzun_temporal_ani_baslangic_orani")),
            -gate._number(item.get("son_cift_bsi_degisim")),
            gate._number(item.get("alan_m2")),
        )
    )

    return {
        "rapor_tarihi": report_date,
        "amac": (
            "15 Eylül sonrası kuru-zemin teyit filtresini yasak döneminde gölge modunda "
            "ölçmek; operasyonel alarm/görev üretmeden güçlü aday hacmini kalibre etmek."
        ),
        "ana_alarm_alt_esigi_m2": gate.MAIN_ALARM_MIN_M2,
        "diagnostik_aralik_m2": [gate.MAIN_ALARM_MIN_M2, gate.DRY_GROUND_MAX_M2],
        "operasyonel": False,
        "alarm": False,
        "kalici_saha_gorevi": False,
        "kurallar": (
            "uzun-temporal ani başlangıç + kararlı geçmiş zemin + yörünge güveni + "
            "izole/non-lineer geometri + lokal 5x5 çevre kontrastı; ana görev 40 m içindeyse atla"
        ),
        "bolgeler": region_summary,
        "guclu_golge_aday_sayisi": len(candidates),
        "adaylar": candidates,
    }


def write_shadow():
    payload = build_shadow(_load(REPORT_JSON), _load(TEMPORAL_AUDIT), _load(LOCALITY_AUDIT))
    if payload is None:
        raise RuntimeError("Gölge kalibrasyon için rapor ve temporal/locality audit tarihleri eşleşmiyor.")
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    before = OUTPUT_JSON.read_text(encoding="utf-8") if OUTPUT_JSON.exists() else ""
    if before != rendered:
        OUTPUT_JSON.write_text(rendered, encoding="utf-8")
        return True, payload
    return False, payload


def _self_check():
    west = gate.freshness.CANONICAL_WEST_REGION
    temporal_item = {
        "mahalle": "Ovacık", "enlem": 38.25, "boylam": 26.32, "alan_m2": 500,
        "ani_baslangic_destegi": True, "istikrarsiz_zemin_riski": False,
        "uzun_temporal_istikrarsiz_zemin_riski": False, "uzun_temporal_koruma": "KORUNDU",
        "yorunge_geometri_riski": False, "izole_saha_benzeri": True,
        "lineer_geometri_riski": False, "uzun_temporal_ani_baslangic_orani": 8.0,
        "son_cift_bsi_degisim": 0.20,
    }
    locality_item = {
        "mahalle": "Ovacık", "enlem": 38.25, "boylam": 26.32, "alan_m2": 500,
        "lokal_ani_baslangic_destegi": True, "yaygin_cevre_degisim_riski": False,
        "yerellik_orani": 2.2,
    }
    report = {"rapor_tarihi": "2026-09-04", "saha_adaylari": []}
    temporal = {"rapor_tarihi": "2026-09-04", "bolgeler": {"cesme": {
        "durum": "ok", "bolge": west, "son_item": "S2_TEST", "son_tarih": "03.09.2026",
        "adaylar": [temporal_item],
    }}}
    locality = {"rapor_tarihi": "2026-09-04", "bolgeler": {"cesme": {
        "durum": "ok", "bolge": west, "son_item": "S2_TEST", "adaylar": [locality_item],
    }}}
    payload = build_shadow(report, temporal, locality)
    assert payload and payload["guclu_golge_aday_sayisi"] == 1
    assert payload["adaylar"][0]["alarm"] is False
    assert payload["ana_alarm_alt_esigi_m2"] == 250

    duplicate = {**report, "saha_adaylari": [{"enlem": 38.2501, "boylam": 26.3201}]}
    payload_dup = build_shadow(duplicate, temporal, locality)
    assert payload_dup and payload_dup["guclu_golge_aday_sayisi"] == 0
    assert payload_dup["bolgeler"]["cesme"]["ana_goreve_yakin_oldugu_icin_atlanan"] == 1

    broad = json.loads(json.dumps(locality))
    broad["bolgeler"]["cesme"]["adaylar"][0]["yaygin_cevre_degisim_riski"] = True
    broad["bolgeler"]["cesme"]["adaylar"][0]["lokal_ani_baslangic_destegi"] = False
    payload_broad = build_shadow(report, temporal, broad)
    assert payload_broad and payload_broad["guclu_golge_aday_sayisi"] == 0
    print("preseason_dry_ground_shadow self-check: OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        return
    changed, payload = write_shadow()
    print(
        "Ön sezon kuru-zemin gölge kalibrasyonu: "
        f"güçlü_aday={payload['guclu_golge_aday_sayisi']}, dosya_değişti={changed}"
    )


if __name__ == "__main__":
    main()
