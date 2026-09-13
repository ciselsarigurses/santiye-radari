"""250+ m2 orta-kuvvette lokal Sentinel-1 RTC sinyallerini ucuncu sahneyle ayir.

Ana RTC katmani tek bir ayni-geometri sahne ciftiyle lokal degisimi olcer. 2 dB ve
uzerindeki guclu kompakt/lokal sinyaller mevcut temporal guard tarafindan zaten
incelenir. Bu katman, erken hafriyat/temel hareketinin ilk geciste 1.5-2.0 dB
bandinda kalabilmesi ihtimaline karsi yalniz 250-6500 m2, cift-polarizasyonlu ve
lokal karakterli ANA adaylari ucuncu sahneyle kontrol eder.

Tek araliklik orta sinyal aktif harita adayi olarak birakilmaz: temporal ani
baslangic veya ardisik lokal hareket destegi yoksa dusuk-kanit arka planina
alinir. Destek varsa ayri SAR_ORTA_TEMPORAL_DESTEKLI diagnostik katmaninda
kalir. Bu katman alarm veya saha gorevi uretmez, 250 m2 ana esigi dusurmez ve
150-249 m2 MIKRO katmanina kesinlikle dokunmaz. Sahada dogrulanmis yikim
onculeri mevcut sentinel1_rtc_temporal_guard.py tarafindan yonetildigi icin burada
tekrar islenmez.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import requests

import sentinel1_blind_target_bridge as bridge
import sentinel1_rtc_change_diagnostic as rtc
import sentinel1_rtc_temporal_guard as temporal
import sentinel1_scene_probe as s1

MIN_MAIN_M2 = 250
MICRO_RANGE_M2 = [150, 249]
TARGET_MAX_AREA_M2 = 6500
MEDIUM_MIN_DB = 1.5
STRONG_LOCAL_DB = 2.0
PREVIOUS_QUIET_MAX_DB = 0.75
PREVIOUS_CONTINUING_MIN_DB = 1.0
EXPECTED_REGIONS = {"cesme", "uzunkuyu", "gulbahce"}
LOCAL_CLASSES = {"KOMPAKT_LOKAL_DESTEKLI", "LOKAL_AYRIM_DESTEKLI"}
BROAD_CLASSES = {
    "GENIS_CEVRE_HAREKETI_BASKIN",
    "GENIS_CEVRE_DEGISIMI_ESLIK_EDIYOR",
    "TEK_POL_CEVRE_DEGISIMI",
}
ALLOWED_POL_CLASSES = {"CIFT_POL_ORTA", "CIFT_POL_GUCLU"}
SUPPORTED_TEMPORAL = {
    "ANI_YENI_ORTA_LOKAL_BASLANGIC_DIAGNOSTIK",
    "ARDISIK_ORTA_LOKAL_HAREKET_DIAGNOSTIK",
}


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coord_key(region, lat, lon):
    try:
        return str(region or "").lower(), round(float(lat), 6), round(float(lon), 6)
    except (TypeError, ValueError):
        return None


def _selection_mode(target):
    """Yalniz ana bantta, insaat-olcekli, orta ve lokal cift-pol hedefleri sec."""
    area = _num(target.get("alan_m2"))
    score = _num(target.get("sar_lokal_degisim_skor_db"))
    if area is None or score is None:
        return None
    if area < MIN_MAIN_M2 or area > TARGET_MAX_AREA_M2:
        return None
    if temporal._is_field_precursor(target):
        return None
    if score < MEDIUM_MIN_DB or score >= STRONG_LOCAL_DB:
        return None
    if str(target.get("sar_polarizasyon_uyumu") or "") not in ALLOWED_POL_CLASSES:
        return None
    if str(target.get("sar_mekansal_ayrim") or "") not in LOCAL_CLASSES:
        return None
    return "ANA_ORTA_LOKAL_TEMPORAL"


def _eligible_targets(region_row):
    selected = []
    for target in region_row.get("hedefler") or []:
        if _selection_mode(target) is None:
            continue
        row = dict(target)
        row["_temporal_secim_nedeni"] = "ANA_ORTA_LOKAL_TEMPORAL"
        selected.append(row)
    return selected


def _classify(previous_evidence, previous_locality):
    prev_score = _num(previous_evidence.get("konservatif_skor_db"))
    prev_locality = str(previous_locality.get("durum") or "VERI_YOK")
    if prev_score is None:
        return "ONCEKI_ARALIK_METRIK_YOK"
    if prev_locality in BROAD_CLASSES:
        return "ONCEKI_ARALIK_GENIS_YUZEY_ETKILI"
    if prev_score < PREVIOUS_QUIET_MAX_DB:
        return "ANI_YENI_ORTA_LOKAL_BASLANGIC_DIAGNOSTIK"
    if prev_score >= PREVIOUS_CONTINUING_MIN_DB and prev_locality in LOCAL_CLASSES:
        return "ARDISIK_ORTA_LOKAL_HAREKET_DIAGNOSTIK"
    return "ORTA_TEMPORAL_KARISIK_DUSUK_KANIT"


def inspect_region(region_row, search_fn=rtc._query_rtc_items, read_fn=rtc._read_target_patch):
    region_key = str(region_row.get("bolge") or "").lower()
    targets = _eligible_targets(region_row)
    base = {
        "bolge": region_key,
        "incelenecek_orta_lokal_hedef": len(targets),
        "alarm": False,
        "saha_gorevi": False,
    }
    if not targets:
        return {**base, "durum": "ORTA_TEMPORAL_HEDEF_YOK", "hedefler": []}

    try:
        items = search_fn(s1.AOIS[region_key]["bbox"])
    except requests.RequestException as exc:
        return {
            **base,
            "durum": "RTC_KAYNAK_GECICI_HATA",
            "hata": type(exc).__name__,
            "hedefler": [],
        }

    current_old = temporal._find_item(items, region_row.get("eski_item"))
    current_new = temporal._find_item(items, region_row.get("yeni_item"))
    if current_old is None or current_new is None:
        return {**base, "durum": "ANA_RTC_CIFTI_YENIDEN_BULUNAMADI", "hedefler": []}

    checked = []
    for target in targets:
        row = {
            "enlem": target.get("enlem"),
            "boylam": target.get("boylam"),
            "alan_m2": target.get("alan_m2"),
            "kaynak": target.get("kaynak"),
            "hedef_katmani": target.get("hedef_katmani"),
            "temporal_secim_nedeni": "ANA_ORTA_LOKAL_TEMPORAL",
            "guncel_aralik_eski_tarih": region_row.get("eski_tarih"),
            "guncel_aralik_yeni_tarih": region_row.get("yeni_tarih"),
            "guncel_sar_lokal_degisim_skor_db": target.get("sar_lokal_degisim_skor_db"),
            "guncel_sar_polarizasyon_uyumu": target.get("sar_polarizasyon_uyumu"),
            "guncel_sar_mekansal_ayrim": target.get("sar_mekansal_ayrim"),
            "alarm": False,
            "saha_gorevi": False,
        }
        predecessor = temporal._find_predecessor(items, region_key, target, current_old)
        if predecessor is None:
            row["temporal_durum"] = "UCUNCU_UYUMLU_SAHNE_YOK"
            checked.append(row)
            continue

        raw = target.get("ham_ozet") or {}
        previous_metrics = {}
        errors = {}
        common_pols, _, _ = bridge._common_raster_polarizations(predecessor, current_old)
        for pol in common_pols:
            if pol not in ("VV", "VH"):
                continue
            old_summary = ((raw.get(pol) or {}).get("eski"))
            if not isinstance(old_summary, dict):
                errors[pol] = "ANA_DIAGNOSTIK_ESKI_OZETI_YOK"
                continue
            prev_summary, prev_error = read_fn(predecessor, pol, target)
            if prev_error:
                errors[pol] = prev_error
                continue
            metric = rtc._metric(prev_summary, old_summary)
            if metric:
                previous_metrics[pol] = metric

        evidence = rtc._polarization_evidence(previous_metrics)
        locality = rtc._spatial_locality(previous_metrics)
        prev_dt = s1._iso_datetime(predecessor)
        old_dt = s1._iso_datetime(current_old)
        row.update(
            {
                "onceki_item": predecessor.get("id"),
                "onceki_aralik_eski_tarih": prev_dt.date().isoformat() if prev_dt else None,
                "onceki_aralik_yeni_tarih": old_dt.date().isoformat() if old_dt else None,
                "onceki_sar_lokal_degisim_skor_db": evidence.get("konservatif_skor_db"),
                "onceki_sar_polarizasyon_uyumu": evidence.get("durum"),
                "onceki_sar_mekansal_ayrim": locality.get("durum"),
                "onceki_sar_cevre_degisim_tepe_db": locality.get("cevre_tepe_db"),
                "temporal_durum": _classify(evidence, locality),
            }
        )
        if previous_metrics:
            row["onceki_polarizasyon_metrikleri"] = previous_metrics
        if errors:
            row["okuma_hatalari"] = errors
        checked.append(row)

    return {
        **base,
        "durum": "ORTA_UC_SAHNE_TEMPORAL_DIAGNOSTIK_HAZIR",
        "ucuncu_sahne_bulunan_hedef": sum(1 for row in checked if row.get("onceki_item")),
        "temporal_destekli_orta_lokal": sum(
            1 for row in checked if row.get("temporal_durum") in SUPPORTED_TEMPORAL
        ),
        "hedefler": checked,
    }


def inspect_payload(payload, search_fn=rtc._query_rtc_items, read_fn=rtc._read_target_patch):
    if payload.get("alarm") is not False or payload.get("saha_gorevi") is not False:
        raise ValueError("RTC medium temporal input must remain alarm/task free")
    if int(payload.get("ana_sentinel_esigi_m2", MIN_MAIN_M2)) != MIN_MAIN_M2:
        raise ValueError("Main Sentinel threshold must remain 250 m2")
    if list(payload.get("mikro_aralik_m2", MICRO_RANGE_M2)) != MICRO_RANGE_M2:
        raise ValueError("MIKRO range must remain 150-249 m2")

    regions = payload.get("bolgeler") or []
    present = {str(row.get("bolge") or "").lower() for row in regions}
    missing = sorted(EXPECTED_REGIONS - present)
    if missing:
        raise ValueError(f"RTC medium temporal diagnostic missing required regions: {missing}")

    rows = [inspect_region(row, search_fn=search_fn, read_fn=read_fn) for row in regions]
    return {
        "durum": "RTC_ORTA_UC_SAHNE_TEMPORAL_KONTROL_HAZIR",
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": MIN_MAIN_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "orta_lokal_skor_araligi_db": [MEDIUM_MIN_DB, STRONG_LOCAL_DB],
        "ana_hedef_alan_araligi_m2": [MIN_MAIN_M2, TARGET_MAX_AREA_M2],
        "kapsanan_bolgeler": sorted(present & EXPECTED_REGIONS),
        "incelenen_orta_lokal_hedef": sum(int(row.get("incelenecek_orta_lokal_hedef") or 0) for row in rows),
        "ucuncu_sahne_bulunan_hedef": sum(int(row.get("ucuncu_sahne_bulunan_hedef") or 0) for row in rows),
        "temporal_destekli_orta_lokal": sum(int(row.get("temporal_destekli_orta_lokal") or 0) for row in rows),
        "bolgeler": rows,
        "not": (
            "Yalniz 250-6500 m2, 1.5-2.0 dB, cift-pol ve lokal ANA sinyaller ucuncu sahneyle "
            "ayrilir; MIKRO ve saha-onculu bu katmana girmez. Sonuc diagnostiktir."
        ),
    }


def _diagnostic_index(payload):
    index = {}
    for region in (payload or {}).get("bolgeler") or []:
        region_key = str(region.get("bolge") or "").lower()
        for target in region.get("hedefler") or []:
            key = _coord_key(region_key, target.get("enlem"), target.get("boylam"))
            if key is not None:
                index[key] = target
    return index


def overlay_map(base_map, diagnostic):
    """Orta tek-aralik sinyali temporal kanita gore aktif diagnostik veya arka plan yap."""
    result = json.loads(json.dumps(base_map))
    index = _diagnostic_index(diagnostic)
    matched = 0
    supported = 0
    demoted = 0

    for feature in result.get("features") or []:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        if str(geometry.get("type") or "") != "Point" or len(coords) < 2:
            continue
        key = _coord_key(props.get("bolge"), coords[1], coords[0])
        row = index.get(key)
        if row is None:
            continue

        matched += 1
        temporal_status = str(row.get("temporal_durum") or "")
        props["orta_temporal_kontrol"] = True
        props["orta_temporal_durum"] = temporal_status
        props["orta_temporal_onceki_skor_db"] = row.get("onceki_sar_lokal_degisim_skor_db")
        props["orta_temporal_onceki_mekansal_ayrim"] = row.get("onceki_sar_mekansal_ayrim")
        props["alarm"] = False
        props["saha_gorevi"] = False

        if temporal_status in SUPPORTED_TEMPORAL:
            supported += 1
            props["harita_katmani"] = "SAR_ORTA_TEMPORAL_DESTEKLI"
            props["arka_plan"] = False
            props["harita_aciklamasi"] = (
                "Orta kuvvette cift-pol lokal SAR degisimi ucuncu sahne temporal destegi aldi; "
                "erken zemin diagnostigi, alarm degil."
            )
        else:
            demoted += 1
            props["harita_katmani"] = "SAR_DUSUK_KANIT_ARKA_PLAN"
            props["arka_plan"] = True
            props["harita_aciklamasi"] = (
                "Orta kuvvette lokal SAR tek basina yeterli degil; ucuncu sahne temporal destegi "
                "yok veya karisik. Arka planda izleniyor."
            )
        feature["properties"] = props

    return result, {"eslesen": matched, "temporal_destekli": supported, "arka_plana_alinan": demoted}


def _write_overlay(base_map_path, output_path, diagnostic):
    base_path = Path(base_map_path)
    output = Path(output_path)
    if int(diagnostic.get("incelenen_orta_lokal_hedef") or 0) == 0:
        shutil.copyfile(base_path, output)
        return {"eslesen": 0, "temporal_destekli": 0, "arka_plana_alinan": 0}
    base_map = json.loads(base_path.read_text(encoding="utf-8"))
    overlaid, stats = overlay_map(base_map, diagnostic)
    output.write_text(json.dumps(overlaid, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return stats


def self_check():
    base = {
        "alan_m2": 400,
        "sar_lokal_degisim_skor_db": 1.7,
        "sar_polarizasyon_uyumu": "CIFT_POL_ORTA",
        "sar_mekansal_ayrim": "LOKAL_AYRIM_DESTEKLI",
        "hedef_katmani": "ANA_250_PLUS",
    }
    assert _selection_mode(base) == "ANA_ORTA_LOKAL_TEMPORAL"
    assert _selection_mode({**base, "alan_m2": 249}) is None
    assert _selection_mode({**base, "alan_m2": 6501}) is None
    assert _selection_mode({**base, "sar_lokal_degisim_skor_db": 1.49}) is None
    assert _selection_mode({**base, "sar_lokal_degisim_skor_db": 2.0}) is None
    assert _selection_mode({**base, "sar_polarizasyon_uyumu": "TEK_POL_BASKIN"}) is None
    assert _selection_mode({**base, "sar_mekansal_ayrim": "GENIS_CEVRE_HAREKETI_BASKIN"}) is None
    assert _selection_mode({**base, "saha_dogrulanmis_yikim_onculu": True}) is None

    assert _classify(
        {"konservatif_skor_db": 0.4}, {"durum": "KARISIK_DUSUK_LOKALLIK"}
    ) == "ANI_YENI_ORTA_LOKAL_BASLANGIC_DIAGNOSTIK"
    assert _classify(
        {"konservatif_skor_db": 1.2}, {"durum": "KOMPAKT_LOKAL_DESTEKLI"}
    ) == "ARDISIK_ORTA_LOKAL_HAREKET_DIAGNOSTIK"
    assert _classify(
        {"konservatif_skor_db": 0.4}, {"durum": "GENIS_CEVRE_HAREKETI_BASKIN"}
    ) == "ONCEKI_ARALIK_GENIS_YUZEY_ETKILI"

    base_map = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [26.64, 38.34]},
                "properties": {
                    "bolge": "gulbahce",
                    "harita_katmani": "SAR_LOKAL_ORTA",
                    "arka_plan": False,
                    "alarm": False,
                    "saha_gorevi": False,
                },
            }
        ],
    }
    diagnostic = {
        "bolgeler": [
            {
                "bolge": "gulbahce",
                "hedefler": [
                    {
                        "enlem": 38.34,
                        "boylam": 26.64,
                        "temporal_durum": "ANI_YENI_ORTA_LOKAL_BASLANGIC_DIAGNOSTIK",
                        "onceki_sar_lokal_degisim_skor_db": 0.3,
                        "onceki_sar_mekansal_ayrim": "KARISIK_DUSUK_LOKALLIK",
                    }
                ],
            }
        ]
    }
    overlaid, stats = overlay_map(base_map, diagnostic)
    props = overlaid["features"][0]["properties"]
    assert props["harita_katmani"] == "SAR_ORTA_TEMPORAL_DESTEKLI", props
    assert props["arka_plan"] is False and props["alarm"] is False and props["saha_gorevi"] is False
    assert stats == {"eslesen": 1, "temporal_destekli": 1, "arka_plana_alinan": 0}, stats

    diagnostic["bolgeler"][0]["hedefler"][0]["temporal_durum"] = "ORTA_TEMPORAL_KARISIK_DUSUK_KANIT"
    overlaid, stats = overlay_map(base_map, diagnostic)
    props = overlaid["features"][0]["properties"]
    assert props["harita_katmani"] == "SAR_DUSUK_KANIT_ARKA_PLAN" and props["arka_plan"] is True
    assert stats["arka_plana_alinan"] == 1
    print("Sentinel-1 RTC orta temporal guard self-check OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check-only", action="store_true")
    parser.add_argument("--input")
    parser.add_argument("--base-map")
    parser.add_argument("--map-output")
    args = parser.parse_args()

    if args.self_check_only:
        self_check()
        return
    if not args.input:
        parser.error("--input is required")
    if bool(args.base_map) != bool(args.map_output):
        parser.error("--base-map and --map-output must be used together")

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    diagnostic = inspect_payload(payload)
    if args.base_map and args.map_output:
        diagnostic["harita_overlay"] = _write_overlay(args.base_map, args.map_output, diagnostic)
    print(json.dumps(diagnostic, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
