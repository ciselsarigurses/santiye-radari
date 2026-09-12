"""Sentinel-1 RTC hedeflerini ucuncu sahneyle temporal kontrol eder.

Ana RTC diagnostigi yalniz en taze ayni-geometri cifti karsilastirir. Bu katman,
ana diagnostikte guclu ve kompakt/lokal destekli kalan hedeflerde bir onceki
uyumlu RTC sahnesini arayarak iki ardisik zaman araligini ayirir. Boylece yeni
ani baslangic ile daha once de suren lokal hareket veya genis-yuzey etkisini
ayri diagnostik siniflarda tutar.

Sahada dogrulanmis yikim/parsel-temizligi oncul hedefleri ise ana esigi veya
alarm politikasini gevsetmeden, cift-polarizasyonda en az orta degisim varsa
ayni temporal kontrolden gecirir. Bu dar istisna yeni hafriyat/temel hareketini
optik yeni sahneyi beklemeden izlemek icindir; tek-polarizasyon sicramalari
ve zayif degisimler bu ek yola alinmaz.

Katman yalniz diagnostiktir: Sentinel-1 tek basina insaat/kazi kaniti, alarm
veya saha gorevi sayilmaz. 250 m2 ana Sentinel esigini ve 150-249 m2 MIKRO
araligini degistirmez. Tam sahne indirmez; yalniz secilen az sayidaki hedef
icin kucuk bir onceki RTC penceresi okur.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import requests

import sentinel1_blind_target_bridge as bridge
import sentinel1_rtc_change_diagnostic as rtc
import sentinel1_scene_probe as s1

MIN_MAIN_M2 = 250
MICRO_RANGE_M2 = [150, 249]
MIN_PAIR_GAP_HOURS = 24
STRONG_LOCAL_DB = 2.0
PRECURSOR_MIN_DB = 1.0
PRECURSOR_LAYER = "SAHA_ONCUL_SAR_DIAGNOSTIK"
EXPECTED_REGIONS = {"cesme", "uzunkuyu", "gulbahce"}
LOCAL_CLASSES = {"KOMPAKT_LOKAL_DESTEKLI", "LOKAL_AYRIM_DESTEKLI"}
BROAD_CLASSES = {
    "GENIS_CEVRE_HAREKETI_BASKIN",
    "GENIS_CEVRE_DEGISIMI_ESLIK_EDIYOR",
    "TEK_POL_CEVRE_DEGISIMI",
}
BLOCKED_POL_CLASSES = {"TEK_POL_BASKIN", "TEK_POL_VERISI", "VERI_YOK"}


def _pair_gap_hours(older, newer):
    old_dt = s1._iso_datetime(older)
    new_dt = s1._iso_datetime(newer)
    if old_dt is None or new_dt is None or old_dt >= new_dt:
        return None
    return (new_dt - old_dt).total_seconds() / 3600.0


def _find_item(items, item_id):
    wanted = str(item_id or "")
    if not wanted:
        return None
    for item in items:
        if str(item.get("id") or "") == wanted:
            return item
    return None


def _find_predecessor(items, region_key, target, current_old):
    """Current-old sahnesinden onceki en taze ayni-geometri hedef sahnesi."""
    signature = s1._compatible_signature(current_old)
    current_dt = s1._iso_datetime(current_old)
    if signature is None or current_dt is None:
        return None

    bbox = s1.AOIS[region_key]["bbox"]
    candidates = [item for item in items if s1._usable_item(item, bbox)]
    candidates.sort(
        key=lambda row: s1._iso_datetime(row) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for previous in candidates:
        prev_dt = s1._iso_datetime(previous)
        if prev_dt is None or prev_dt >= current_dt:
            continue
        if s1._compatible_signature(previous) != signature:
            continue
        gap = _pair_gap_hours(previous, current_old)
        if gap is None or gap < MIN_PAIR_GAP_HOURS:
            continue
        if not bridge._target_covered_by_pair(target, previous, current_old):
            continue
        common_pols, _, _ = bridge._common_raster_polarizations(previous, current_old)
        if not any(pol in ("VV", "VH") for pol in common_pols):
            continue
        return previous
    return None


def _temporal_class(current_score, previous_evidence, previous_locality):
    prev_score = previous_evidence.get("konservatif_skor_db")
    prev_local = str(previous_locality.get("durum") or "VERI_YOK")
    if prev_score is None:
        return "ONCEKI_ARALIK_METRIK_YOK"
    prev_score = float(prev_score)
    if prev_local in BROAD_CLASSES:
        if current_score >= STRONG_LOCAL_DB and prev_score < 1.0:
            return "ANI_YENI_LOKAL_BASLANGIC_GENIS_ARKA_PLANLI"
        return "ONCEKI_ARALIK_GENIS_YUZEY_ETKILI"
    if current_score >= STRONG_LOCAL_DB and prev_score < 1.0:
        return "ANI_YENI_LOKAL_BASLANGIC_DESTEKLI"
    if prev_score >= STRONG_LOCAL_DB and prev_local in LOCAL_CLASSES:
        return "ARDISIK_GUCLU_LOKAL_HAREKET"
    if prev_score >= 1.0 and prev_local in LOCAL_CLASSES:
        return "ARDISIK_ORTA_LOKAL_HAREKET"
    return "TEMPORAL_KARISIK_DUSUK_KANIT"


def _is_field_precursor(target):
    return bool(target.get("saha_dogrulanmis_yikim_onculu")) or str(
        target.get("hedef_katmani") or ""
    ) == PRECURSOR_LAYER


def _selection_mode(target):
    try:
        score = float(target.get("sar_lokal_degisim_skor_db"))
    except (TypeError, ValueError):
        return None
    locality = str(target.get("sar_mekansal_ayrim") or "")
    pol = str(target.get("sar_polarizasyon_uyumu") or "")
    if pol in BLOCKED_POL_CLASSES:
        return None
    if score >= STRONG_LOCAL_DB and locality in LOCAL_CLASSES:
        return "GUCLU_LOKAL"
    if (
        _is_field_precursor(target)
        and score >= PRECURSOR_MIN_DB
        and pol in {"CIFT_POL_ORTA", "CIFT_POL_GUCLU"}
    ):
        return "SAHA_ONCUL_ORTA_PLUS"
    return None


def _eligible_targets(region_row):
    selected = []
    for target in region_row.get("hedefler") or []:
        mode = _selection_mode(target)
        if not mode:
            continue
        row = dict(target)
        row["_temporal_secim_nedeni"] = mode
        selected.append(row)
    return selected


def inspect_region(region_row, search_fn=rtc._query_rtc_items, read_fn=rtc._read_target_patch):
    region_key = str(region_row.get("bolge") or "").lower()
    targets = _eligible_targets(region_row)
    strong_count = sum(1 for target in targets if target.get("_temporal_secim_nedeni") == "GUCLU_LOKAL")
    precursor_count = sum(
        1 for target in targets if target.get("_temporal_secim_nedeni") == "SAHA_ONCUL_ORTA_PLUS"
    )
    base = {
        "bolge": region_key,
        "incelenecek_guclu_lokal_hedef": strong_count,
        "incelenecek_saha_oncul_hedef": precursor_count,
        "incelenecek_temporal_hedef": len(targets),
        "alarm": False,
        "saha_gorevi": False,
    }
    if not targets:
        return {**base, "durum": "TEMPORAL_HEDEF_YOK", "hedefler": []}

    try:
        items = search_fn(s1.AOIS[region_key]["bbox"])
    except requests.RequestException as exc:
        return {**base, "durum": "RTC_KAYNAK_GECICI_HATA", "hata": type(exc).__name__, "hedefler": []}

    current_old = _find_item(items, region_row.get("eski_item"))
    current_new = _find_item(items, region_row.get("yeni_item"))
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
            "temporal_secim_nedeni": target.get("_temporal_secim_nedeni"),
            "saha_dogrulanmis_yikim_onculu": bool(target.get("saha_dogrulanmis_yikim_onculu")),
            "saha_oncul_gorev_id": target.get("saha_oncul_gorev_id"),
            "saha_oncul_son_tarih": target.get("saha_oncul_son_tarih"),
            "guncel_aralik_eski_tarih": region_row.get("eski_tarih"),
            "guncel_aralik_yeni_tarih": region_row.get("yeni_tarih"),
            "guncel_sar_lokal_degisim_skor_db": target.get("sar_lokal_degisim_skor_db"),
            "guncel_sar_polarizasyon_uyumu": target.get("sar_polarizasyon_uyumu"),
            "guncel_sar_mekansal_ayrim": target.get("sar_mekansal_ayrim"),
            "alarm": False,
            "saha_gorevi": False,
        }
        predecessor = _find_predecessor(items, region_key, target, current_old)
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
        try:
            current_score = float(target.get("sar_lokal_degisim_skor_db"))
        except (TypeError, ValueError):
            current_score = 0.0
        prev_dt = s1._iso_datetime(predecessor)
        old_dt = s1._iso_datetime(current_old)
        row.update({
            "onceki_item": predecessor.get("id"),
            "onceki_aralik_eski_tarih": prev_dt.date().isoformat() if prev_dt else None,
            "onceki_aralik_yeni_tarih": old_dt.date().isoformat() if old_dt else None,
            "onceki_sar_lokal_degisim_skor_db": evidence.get("konservatif_skor_db"),
            "onceki_sar_polarizasyon_uyumu": evidence.get("durum"),
            "onceki_sar_mekansal_ayrim": locality.get("durum"),
            "onceki_sar_cevre_degisim_tepe_db": locality.get("cevre_tepe_db"),
            "temporal_durum": _temporal_class(current_score, evidence, locality),
        })
        if previous_metrics:
            row["onceki_polarizasyon_metrikleri"] = previous_metrics
        if errors:
            row["okuma_hatalari"] = errors
        checked.append(row)

    return {
        **base,
        "durum": "UC_SAHNE_TEMPORAL_DIAGNOSTIK_HAZIR",
        "ucuncu_sahne_bulunan_hedef": sum(1 for row in checked if row.get("onceki_item")),
        "saha_oncul_ucuncu_sahne_bulunan": sum(
            1
            for row in checked
            if row.get("temporal_secim_nedeni") == "SAHA_ONCUL_ORTA_PLUS" and row.get("onceki_item")
        ),
        "ani_yeni_lokal_baslangic_destekli": sum(
            1 for row in checked if row.get("temporal_durum") == "ANI_YENI_LOKAL_BASLANGIC_DESTEKLI"
        ),
        "genis_arka_planli_ani_lokal_baslangic": sum(
            1 for row in checked if row.get("temporal_durum") == "ANI_YENI_LOKAL_BASLANGIC_GENIS_ARKA_PLANLI"
        ),
        "ardisik_lokal_hareket": sum(
            1 for row in checked if str(row.get("temporal_durum") or "").startswith("ARDISIK_")
        ),
        "hedefler": checked,
    }


def inspect_payload(payload, search_fn=rtc._query_rtc_items, read_fn=rtc._read_target_patch):
    if payload.get("alarm") is not False or payload.get("saha_gorevi") is not False:
        raise ValueError("RTC temporal input must remain alarm/task free")
    if int(payload.get("ana_sentinel_esigi_m2", MIN_MAIN_M2)) != MIN_MAIN_M2:
        raise ValueError("Main Sentinel threshold must remain 250 m2")
    if list(payload.get("mikro_aralik_m2", MICRO_RANGE_M2)) != MICRO_RANGE_M2:
        raise ValueError("MIKRO range must remain 150-249 m2")

    regions = payload.get("bolgeler") or []
    present = {str(row.get("bolge") or "").lower() for row in regions}
    missing = sorted(EXPECTED_REGIONS - present)
    if missing:
        raise ValueError(f"RTC temporal diagnostic missing required regions: {missing}")

    rows = [inspect_region(row, search_fn=search_fn, read_fn=read_fn) for row in regions]
    return {
        "durum": "RTC_UC_SAHNE_TEMPORAL_KONTROL_HAZIR",
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": MIN_MAIN_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "kapsanan_bolgeler": sorted(present & EXPECTED_REGIONS),
        "incelenen_guclu_lokal_hedef": sum(int(row.get("incelenecek_guclu_lokal_hedef") or 0) for row in rows),
        "incelenen_saha_oncul_temporal_hedef": sum(
            int(row.get("incelenecek_saha_oncul_hedef") or 0) for row in rows
        ),
        "incelenen_temporal_hedef": sum(int(row.get("incelenecek_temporal_hedef") or 0) for row in rows),
        "ucuncu_sahne_bulunan_hedef": sum(int(row.get("ucuncu_sahne_bulunan_hedef") or 0) for row in rows),
        "saha_oncul_ucuncu_sahne_bulunan": sum(
            int(row.get("saha_oncul_ucuncu_sahne_bulunan") or 0) for row in rows
        ),
        "ani_yeni_lokal_baslangic_destekli": sum(int(row.get("ani_yeni_lokal_baslangic_destekli") or 0) for row in rows),
        "genis_arka_planli_ani_lokal_baslangic": sum(
            int(row.get("genis_arka_planli_ani_lokal_baslangic") or 0) for row in rows
        ),
        "ardisik_lokal_hareket": sum(int(row.get("ardisik_lokal_hareket") or 0) for row in rows),
        "bolgeler": rows,
        "not": "Uc sahne sonucu yalniz temporal SAR diagnostigidir; tek basina insaat/kazi alarmi veya saha gorevi uretmez.",
    }


def self_check():
    bbox = list(s1.AOIS["gulbahce"]["bbox"])
    target = {"enlem": 38.341406, "boylam": 26.643308, "alan_m2": 1200}

    def fake_item(item_id, iso_date, orbit=29):
        return {
            "id": item_id,
            "bbox": bbox,
            "properties": {
                "datetime": iso_date,
                "sat:relative_orbit": orbit,
                "sat:orbit_state": "ascending",
                "sar:polarizations": ["VV", "VH"],
                "sar:instrument_mode": "IW",
            },
            "assets": {
                "vv": {"href": "https://example.test/vv.tif", "roles": ["data"]},
                "vh": {"href": "https://example.test/vh.tif", "roles": ["data"]},
            },
        }

    prev = fake_item("RTC_PREV", "2026-08-30T16:14:00Z")
    old = fake_item("RTC_OLD", "2026-09-05T16:14:00Z")
    new = fake_item("RTC_NEW", "2026-09-11T16:14:00Z")
    wrong = fake_item("RTC_WRONG", "2026-09-04T16:14:00Z", orbit=40)
    found = _find_predecessor([new, old, wrong, prev], "gulbahce", target, old)
    assert found and found["id"] == "RTC_PREV", found

    assert _temporal_class(
        2.4,
        {"konservatif_skor_db": 0.4},
        {"durum": "KARISIK_DUSUK_LOKALLIK"},
    ) == "ANI_YENI_LOKAL_BASLANGIC_DESTEKLI"
    assert _temporal_class(
        2.4,
        {"konservatif_skor_db": 0.4},
        {"durum": "TEK_POL_CEVRE_DEGISIMI"},
    ) == "ANI_YENI_LOKAL_BASLANGIC_GENIS_ARKA_PLANLI"
    assert _temporal_class(
        2.4,
        {"konservatif_skor_db": 2.1},
        {"durum": "KOMPAKT_LOKAL_DESTEKLI"},
    ) == "ARDISIK_GUCLU_LOKAL_HAREKET"
    assert _temporal_class(
        2.4,
        {"konservatif_skor_db": 2.1},
        {"durum": "GENIS_CEVRE_DEGISIMI_ESLIK_EDIYOR"},
    ) == "ONCEKI_ARALIK_GENIS_YUZEY_ETKILI"

    precursor = {
        "enlem": 38.331547,
        "boylam": 26.644338,
        "alan_m2": 400,
        "hedef_katmani": PRECURSOR_LAYER,
        "saha_dogrulanmis_yikim_onculu": True,
        "sar_lokal_degisim_skor_db": 1.05,
        "sar_polarizasyon_uyumu": "CIFT_POL_ORTA",
        "sar_mekansal_ayrim": "KARISIK_DUSUK_LOKALLIK",
    }
    assert _selection_mode(precursor) == "SAHA_ONCUL_ORTA_PLUS"
    assert _selection_mode({**precursor, "sar_lokal_degisim_skor_db": 0.99}) is None
    assert _selection_mode({**precursor, "sar_polarizasyon_uyumu": "TEK_POL_BASKIN"}) is None
    ordinary_medium = {
        **precursor,
        "hedef_katmani": "ANA_250_PLUS",
        "saha_dogrulanmis_yikim_onculu": False,
    }
    assert _selection_mode(ordinary_medium) is None
    print("Sentinel-1 RTC uc sahne temporal guard self-check OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check-only", action="store_true")
    parser.add_argument("--input")
    args = parser.parse_args()
    if args.self_check_only:
        self_check()
        return
    if not args.input:
        parser.error("--input is required")
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    print(json.dumps(inspect_payload(payload), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
