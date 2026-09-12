"""Sentinel-1 RTC kor hedeflerde hedefe ozel taze cift bosluk tamamlamasi.

Bolge icin secilen tek RTC cifti egik SAR seridi nedeniyle bazi optik kor
hedefleri gercekte kapsamayabilir; ayrica Gülbahçe gibi dar bir operasyon
alaninda bolgesel cift eski kalabilir. Bu katman yalniz bu iki durumda devreye
girer ve her hedef icin tum acik RTC metadata icinden gercek footprint'i hedefi
iki tarihte de kapsayan en taze ayni-geometri cifti arar.

Katman yalniz diagnostiktir. Sentinel-1 geri-sacilim degisimi tek basina
insaat/kazi kaniti, alarm veya saha gorevi sayilmaz. 250 m2 ana uretim esigini
ve 150-249 m2 MIKRO politikasini degistirmez. Tam sahne indirmez; yalniz secilen
hedeflerde kucuk RTC pencereleri okur.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import requests

import sentinel1_blind_target_bridge as bridge
import sentinel1_rtc_change_diagnostic as rtc
import sentinel1_scene_probe as s1


MIN_MAIN_M2 = 250
MICRO_RANGE_M2 = [150, 249]
FRESH_DAYS = 2
MIN_PAIR_GAP_HOURS = 24


def _age_days(item, now=None):
    dt = s1._iso_datetime(item)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, (now.date() - dt.date()).days)


def _pair_gap_hours(older, newer):
    old_dt = s1._iso_datetime(older)
    new_dt = s1._iso_datetime(newer)
    if old_dt is None or new_dt is None or old_dt >= new_dt:
        return None
    return (new_dt - old_dt).total_seconds() / 3600.0


def _usable_items(items, region_key):
    bbox = s1.AOIS[region_key]["bbox"]
    usable = [item for item in items if s1._usable_item(item, bbox)]
    usable.sort(
        key=lambda row: s1._iso_datetime(row) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return usable


def _freshest_target_pair(items, region_key, target):
    """Hedefi gercek footprint ile iki tarihte de kapsayan en taze cifti bul."""
    usable = _usable_items(items, region_key)
    for idx, newer in enumerate(usable):
        signature = s1._compatible_signature(newer)
        new_dt = s1._iso_datetime(newer)
        if signature is None or new_dt is None:
            continue
        for older in usable[idx + 1 :]:
            if s1._compatible_signature(older) != signature:
                continue
            gap_hours = _pair_gap_hours(older, newer)
            if gap_hours is None or gap_hours < MIN_PAIR_GAP_HOURS:
                continue
            if not bridge._target_covered_by_pair(target, older, newer):
                continue
            common_pols, _, _ = bridge._common_raster_polarizations(older, newer)
            common_pols = [pol for pol in common_pols if pol in ("VV", "VH")]
            if not common_pols:
                continue
            return older, newer, common_pols
    return None


def _reference_pair(items, region_key):
    pair = s1._find_same_geometry_pair(items, region_key)
    if not pair:
        return None
    older, newer = pair
    common_pols, _, _ = bridge._common_raster_polarizations(older, newer)
    common_pols = [pol for pol in common_pols if pol in ("VV", "VH")]
    if not common_pols:
        return None
    return older, newer, common_pols


def _needs_gapfill(target, reference_pair, now=None):
    if not reference_pair:
        return True, "BOLGESEL_RTC_CIFTI_YOK"
    older, newer, _ = reference_pair
    if not bridge._target_covered_by_pair(target, older, newer):
        return True, "BOLGESEL_CIFT_GERCEK_FOOTPRINT_DISI"
    age = _age_days(newer, now=now)
    if age is None:
        return True, "BOLGESEL_CIFT_TARIHI_YOK"
    if age > FRESH_DAYS:
        return True, "BOLGESEL_CIFT_ESKI"
    return False, "BOLGESEL_CIFT_GUNCEL_VE_KAPSIYOR"


def _measure_pair(target, pair, read_fn=rtc._read_target_patch):
    older, newer, common_pols = pair
    metrics = {}
    raw = {}
    errors = {}
    for pol in common_pols:
        old_summary, old_error = read_fn(older, pol, target)
        new_summary, new_error = read_fn(newer, pol, target)
        if old_error or new_error:
            errors[pol] = {"eski": old_error, "yeni": new_error}
            continue
        raw[pol] = {"eski": old_summary, "yeni": new_summary}
        metric = rtc._metric(old_summary, new_summary)
        if metric:
            metrics[pol] = metric

    evidence = rtc._polarization_evidence(metrics)
    result = {
        "polarizasyon_metrikleri": metrics,
        "sar_degisim_durumu": rtc._severity(metrics),
        "sar_polarizasyon_uyumu": evidence["durum"],
        "sar_lokal_degisim_skor_db": evidence["konservatif_skor_db"],
        "sar_lokal_degisim_ham_medyan_db": evidence["ham_medyan_db"],
        "sar_lokal_degisim_tepe_db": evidence["tepe_db"],
        "sar_polarizasyon_yayilim_db": evidence["yayilim_db"],
    }
    if raw:
        result["ham_ozet"] = raw
    if errors:
        result["okuma_hatalari"] = errors
    return result


def inspect_region(region_key, targets, search_fn=rtc._query_rtc_items, read_fn=rtc._read_target_patch, now=None):
    now = now or datetime.now(timezone.utc)
    aoi = s1.AOIS[region_key]
    try:
        items = search_fn(aoi["bbox"])
    except requests.RequestException as exc:
        return {
            "bolge": region_key,
            "durum": "RTC_KAYNAK_GECICI_HATA",
            "hata": type(exc).__name__,
            "hedef_sayisi": len(targets),
            "alarm": False,
            "saha_gorevi": False,
        }

    reference = _reference_pair(items, region_key)
    candidates = []
    skipped = 0
    for target in targets:
        needed, reason = _needs_gapfill(target, reference, now=now)
        if not needed:
            skipped += 1
            continue

        row = dict(target)
        row.update({
            "gapfill_nedeni": reason,
            "alarm": False,
            "saha_gorevi": False,
        })
        pair = _freshest_target_pair(items, region_key, target)
        if not pair:
            row.update({
                "gapfill_durumu": "HEDEFE_OZEL_KARSILASTIRILABILIR_CIFT_YOK",
                "rtc_hedef_cifti_kapsiyor": False,
            })
            candidates.append(row)
            continue

        older, newer, common_pols = pair
        new_dt = s1._iso_datetime(newer)
        old_dt = s1._iso_datetime(older)
        age = _age_days(newer, now=now)
        row.update({
            "gapfill_durumu": "HEDEFE_OZEL_CIFT_BULUNDU",
            "rtc_hedef_cifti_kapsiyor": True,
            "rtc_eski_item": older.get("id"),
            "rtc_eski_tarih": old_dt.date().isoformat() if old_dt else None,
            "rtc_yeni_item": newer.get("id"),
            "rtc_yeni_tarih": new_dt.date().isoformat() if new_dt else None,
            "rtc_cifti_yasi_gun": age,
            "rtc_tazelik": "RTC_GUNCEL" if age is not None and age <= FRESH_DAYS else "RTC_ESKIMIS",
            "goreli_yorunge": s1._relative_orbit(newer),
            "orbit_yonu": s1._orbit_state(newer),
            "polarizasyonlar": common_pols,
            "cift_araligi_saat": round(_pair_gap_hours(older, newer) or 0.0, 1),
        })
        row.update(_measure_pair(target, pair, read_fn=read_fn))
        candidates.append(row)

    found = [r for r in candidates if r.get("rtc_hedef_cifti_kapsiyor")]
    fresh = [r for r in found if r.get("rtc_tazelik") == "RTC_GUNCEL"]
    measured = [r for r in found if r.get("sar_lokal_degisim_skor_db") is not None]
    fresh_measured = [r for r in measured if r.get("rtc_tazelik") == "RTC_GUNCEL"]
    ranked = sorted(
        fresh_measured,
        key=lambda row: float(row.get("sar_lokal_degisim_skor_db") or 0.0),
        reverse=True,
    )

    if not candidates:
        status = "GAPFILL_GEREKMIYOR"
    elif fresh_measured:
        status = "TAZE_HEDEFE_OZEL_RTC_TAMAMLAMASI_VAR"
    elif fresh:
        status = "TAZE_CIFT_VAR_METRIK_YOK"
    elif found:
        status = "YALNIZ_ESKI_HEDEFE_OZEL_CIFT_VAR"
    else:
        status = "HEDEFE_OZEL_RTC_CIFT_BULUNAMADI"

    reference_newer = reference[1] if reference else None
    reference_dt = s1._iso_datetime(reference_newer) if reference_newer else None
    return {
        "bolge": region_key,
        "durum": status,
        "hedef_sayisi": len(targets),
        "gapfill_gerekmeyen_hedef": skipped,
        "gapfill_incelenen_hedef": len(candidates),
        "hedefe_ozel_cift_bulunan": len(found),
        "taze_hedefe_ozel_cift": len(fresh),
        "metrik_uretilen_gapfill_hedef": len(measured),
        "taze_metrik_uretilen_gapfill_hedef": len(fresh_measured),
        "bolgesel_referans_yeni_tarih": reference_dt.date().isoformat() if reference_dt else None,
        "en_yuksek_taze_gapfill_diagnostikler": [
            {
                "enlem": row["enlem"],
                "boylam": row["boylam"],
                "alan_m2": row["alan_m2"],
                "gapfill_nedeni": row["gapfill_nedeni"],
                "rtc_yeni_tarih": row.get("rtc_yeni_tarih"),
                "goreli_yorunge": row.get("goreli_yorunge"),
                "orbit_yonu": row.get("orbit_yonu"),
                "sar_degisim_durumu": row.get("sar_degisim_durumu"),
                "sar_polarizasyon_uyumu": row.get("sar_polarizasyon_uyumu"),
                "sar_lokal_degisim_skor_db": row.get("sar_lokal_degisim_skor_db"),
            }
            for row in ranked[:5]
        ],
        "hedefler": candidates,
        "alarm": False,
        "saha_gorevi": False,
    }


def _self_check():
    bbox = list(s1.AOIS["gulbahce"]["bbox"])
    target = {
        "enlem": 38.341406,
        "boylam": 26.643308,
        "alan_m2": 1200,
        "kaynak": "test",
        "neden": "BULUT",
        "mahalle_yaklasik": "Gulbahce",
    }

    def fake(day, orbit=29, state="ascending"):
        return {
            "id": f"RTC_{orbit}_{day:02d}",
            "bbox": bbox,
            "properties": {
                "datetime": f"2026-09-{day:02d}T16:14:00Z",
                "sat:relative_orbit": orbit,
                "sat:orbit_state": state,
                "sar:polarizations": ["VV", "VH"],
                "sar:instrument_mode": "IW",
            },
            "assets": {
                "vv": {"href": "https://example.test/vv.tif", "roles": ["data"]},
                "vh": {"href": "https://example.test/vh.tif", "roles": ["data"]},
            },
        }

    items = [fake(11), fake(5), fake(11, orbit=40), fake(10, orbit=40)]
    pair = _freshest_target_pair(items, "gulbahce", target)
    assert pair is not None
    assert s1._iso_datetime(pair[1]).date().isoformat() == "2026-09-11"
    # Ayni gun/gecise ait parcalar en az 24 saatlik temporal cift sayilmaz.
    assert s1._relative_orbit(pair[1]) == 29

    def fake_search(_bbox):
        return [fake(11), fake(5)]

    def fake_read(item, pol, _target):
        if item["id"].endswith("05"):
            return {"merkez_db": 0.0, "cevre_db": 0.0, "lokal_kontrast_db": 0.0}, None
        return {"merkez_db": 3.5, "cevre_db": 0.0, "lokal_kontrast_db": 3.5}, None

    # Referans cift taze ve hedefi kapsiyorsa gereksiz ikinci okuma yapma.
    result = inspect_region(
        "gulbahce",
        [target],
        search_fn=fake_search,
        read_fn=fake_read,
        now=datetime(2026, 9, 12, tzinfo=timezone.utc),
    )
    assert result["durum"] == "GAPFILL_GEREKMIYOR", result
    assert result["gapfill_incelenen_hedef"] == 0
    assert result["alarm"] is False and result["saha_gorevi"] is False
    print("Sentinel-1 RTC hedefe ozel cift gapfill oz testi OK.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check-only", action="store_true")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    if args.self_check_only:
        _self_check()
        return

    targets = bridge._load_targets()
    rows = []
    for region_key in ("cesme", "uzunkuyu", "gulbahce"):
        try:
            rows.append(inspect_region(region_key, targets.get(region_key) or []))
        except Exception as exc:
            rows.append({
                "bolge": region_key,
                "durum": "GAPFILL_DIAGNOSTIK_HATA",
                "hata": type(exc).__name__,
                "hedef_sayisi": len(targets.get(region_key) or []),
                "alarm": False,
                "saha_gorevi": False,
            })

    payload = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": MIN_MAIN_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "amac": "Bolgesel RTC ciftinin gercek footprint disinda veya eski biraktigi optik kor hedeflerde, hedefe ozel en taze ayni-geometri RTC ciftini arayip yalniz diagnostik lokal degisimi olcmek.",
        "bolgeler": rows,
        "toplam_gapfill_incelenen_hedef": sum(int(row.get("gapfill_incelenen_hedef") or 0) for row in rows),
        "toplam_taze_hedefe_ozel_cift": sum(int(row.get("taze_hedefe_ozel_cift") or 0) for row in rows),
        "toplam_taze_metrik_uretilen_gapfill_hedef": sum(int(row.get("taze_metrik_uretilen_gapfill_hedef") or 0) for row in rows),
        "not": "Bu katman tek basina alarm veya saha gorevi uretmez; SAR esikleri saha kalibrasyonu olmadan uretim kararina baglanmaz.",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
