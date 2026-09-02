"""150-249 m² mikro şantiye adaylarına üç-Sentinel-sahne zaman serisi kanıtı ekler.

Bu katman yalnız diagnostiktir: alarm, saha görevi veya ana 250 m² üretim eşiğini
DEĞİŞTİRMEZ. Son mikro kısa listedeki adayın bulunduğu 3x3 Sentinel yamasında
"değişim öncesi -> önceki" ve "önceki -> son" dönemlerini aynı geçerli piksellerle
karşılaştırır. Amaç tek karelik tarla/toprak parazitini, ani başlayan lokal müdahaleden
ve iki ardışık dönemde devam eden hareketten ayırmaya yardımcı olmaktır.

Gülbahçe açık bir kapsama/raporlama referansıdır; mahalle sınırı, adres, imar veya
parsel doğrulaması değildir.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

import satellite


SHORTLIST_FILE = Path(__file__).with_name("micro_site_shortlist.json")
RAW_AUDIT_FILE = Path(__file__).with_name("micro_site_audit.json")
OUTPUT_FILE = Path(__file__).with_name("micro_site_temporal_review.json")
ISTANBUL = ZoneInfo("Europe/Istanbul")

PATCH_RADIUS_PIXELS = 1
MIN_VALID_FRACTION = 2 / 3
MIN_PREVIOUS_GAP_DAYS = 2
ABRUPT_RATIO_MIN = 1.8
ABRUPT_PREVIOUS_RELATIVE_MAX = 0.65
CONTINUING_PREVIOUS_SCORE_MIN = 0.18
CONTINUING_CURRENT_SCORE_MIN = 0.30
UNSTABLE_PREVIOUS_RELATIVE_MIN = 0.80
GULBAHCE_REFERENCE_POINT = (38.319473, 26.646463)
GULBAHCE_LABEL_RADIUS_M = 5_000
REQUIRED_ASSETS = ("visual", "red", "nir", "scl")


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _item_time(item):
    raw = str(item.get("properties", {}).get("datetime") or "")
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _find_item(items, item_id):
    wanted = str(item_id or "")
    return next((item for item in items if str(item.get("id") or "") == wanted), None)


def _usable(item, bbox):
    assets = item.get("assets", {})
    return satellite._item_covers_bbox(item, bbox) and all(
        key in assets for key in REQUIRED_ASSETS
    )


def _previous_scene(items, older, bbox):
    """Mevcut çiftin eski sahnesinden önceki güvenli, tam-kapsam sahneyi seç."""
    older_time = _item_time(older)
    if older_time is None:
        return None

    candidates = []
    for item in items:
        if item is older or not _usable(item, bbox):
            continue
        item_time = _item_time(item)
        if item_time is None:
            continue
        gap_days = (older_time - item_time).total_seconds() / 86400
        if gap_days < MIN_PREVIOUS_GAP_DAYS:
            continue
        if not satellite._same_mgrs_tile(item, older):
            continue
        candidates.append(item)

    if not candidates:
        return None

    older_orbit = satellite._relative_orbit(older)
    if older_orbit is not None:
        same_orbit = [
            item for item in candidates
            if satellite._relative_orbit(item) == older_orbit
        ]
        if same_orbit:
            candidates = same_orbit

    return max(candidates, key=lambda item: _item_time(item))


def _pixel_for_point(latitude, longitude, bbox, shape):
    height, width = shape
    west, south, east, north = map(float, bbox)
    row = int((north - float(latitude)) / (north - south) * height)
    column = int((float(longitude) - west) / (east - west) * width)
    row = min(max(row, 0), height - 1)
    column = min(max(column, 0), width - 1)
    return row, column


def _patch_slices(row, column, shape):
    height, width = shape
    radius = PATCH_RADIUS_PIXELS
    return (
        slice(max(0, row - radius), min(height, row + radius + 1)),
        slice(max(0, column - radius), min(width, column + radius + 1)),
    )


def _scene_arrays(item, bbox, height, width):
    visual = satellite._read_asset(
        item, "visual", bbox, height, width, "bilinear"
    )[:3]
    red = satellite._reflectance(
        satellite._read_asset(item, "red", bbox, height, width, "bilinear")[0]
    )
    nir = satellite._reflectance(
        satellite._read_asset(item, "nir", bbox, height, width, "bilinear")[0]
    )
    scl = satellite._read_asset(
        item, "scl", bbox, height, width, "nearest"
    )[0]
    rgb = np.moveaxis(visual, 0, 2).astype("float32") / 255
    ndvi = satellite._ndvi(red, nir)
    brightness = np.mean(rgb, axis=2)
    return rgb, ndvi, brightness, scl


def _paired_metrics(previous, older, latest, valid_mask, row_slice, col_slice):
    previous_rgb, previous_ndvi, previous_brightness = previous
    older_rgb, older_ndvi, older_brightness = older
    latest_rgb, latest_ndvi, latest_brightness = latest

    patch_valid = valid_mask[row_slice, col_slice]
    total = int(patch_valid.size)
    valid_count = int(patch_valid.sum())
    valid_fraction = valid_count / max(total, 1)
    if not valid_count:
        return None

    prev_rgb_delta = np.mean(
        np.abs(older_rgb - previous_rgb), axis=2
    )[row_slice, col_slice]
    curr_rgb_delta = np.mean(
        np.abs(latest_rgb - older_rgb), axis=2
    )[row_slice, col_slice]
    prev_ndvi_loss = (previous_ndvi - older_ndvi)[row_slice, col_slice]
    curr_ndvi_loss = (older_ndvi - latest_ndvi)[row_slice, col_slice]
    prev_brightness_gain = (older_brightness - previous_brightness)[row_slice, col_slice]
    curr_brightness_gain = (latest_brightness - older_brightness)[row_slice, col_slice]

    def mean_valid(array):
        return float(np.mean(array[patch_valid]))

    return {
        "valid_fraction": valid_fraction,
        "previous_rgb_change": mean_valid(prev_rgb_delta),
        "current_rgb_change": mean_valid(curr_rgb_delta),
        "previous_ndvi_loss": mean_valid(prev_ndvi_loss),
        "current_ndvi_loss": mean_valid(curr_ndvi_loss),
        "previous_brightness_gain": mean_valid(prev_brightness_gain),
        "current_brightness_gain": mean_valid(curr_brightness_gain),
    }


def _change_score(rgb_change, ndvi_loss, brightness_gain):
    return (
        max(float(rgb_change or 0), 0.0)
        + 0.7 * max(float(ndvi_loss or 0), 0.0)
        + 0.3 * max(float(brightness_gain or 0), 0.0)
    )


def _classify(metrics):
    if not metrics:
        return {
            "previous_score": None,
            "current_score": None,
            "ratio": None,
            "abrupt": False,
            "continuing": False,
            "unstable": False,
            "label": "YETERSIZ_GECERLI_VERI",
        }

    valid = float(metrics.get("valid_fraction") or 0)
    previous_score = _change_score(
        metrics.get("previous_rgb_change"),
        metrics.get("previous_ndvi_loss"),
        metrics.get("previous_brightness_gain"),
    )
    current_score = _change_score(
        metrics.get("current_rgb_change"),
        metrics.get("current_ndvi_loss"),
        metrics.get("current_brightness_gain"),
    )
    ratio = current_score / max(previous_score, 0.01)

    abrupt = bool(
        valid >= MIN_VALID_FRACTION
        and current_score >= CONTINUING_CURRENT_SCORE_MIN
        and ratio >= ABRUPT_RATIO_MIN
        and previous_score <= current_score * ABRUPT_PREVIOUS_RELATIVE_MAX
    )
    continuing = bool(
        valid >= MIN_VALID_FRACTION
        and previous_score >= CONTINUING_PREVIOUS_SCORE_MIN
        and current_score >= CONTINUING_CURRENT_SCORE_MIN
    )
    unstable = bool(
        valid >= MIN_VALID_FRACTION
        and previous_score >= current_score * UNSTABLE_PREVIOUS_RELATIVE_MIN
    )

    if abrupt:
        label = "ANI_BASLANGIC_DESTEGI"
    elif continuing:
        label = "DEVAM_EDEN_HAREKET"
    elif valid < MIN_VALID_FRACTION:
        label = "YETERSIZ_GECERLI_VERI"
    else:
        label = "TEK_DONEM_SINYALI"

    return {
        "previous_score": round(previous_score, 4),
        "current_score": round(current_score, 4),
        "ratio": round(ratio, 2),
        "abrupt": abrupt,
        "continuing": continuing,
        "unstable": unstable,
        "label": label,
    }


def _distance_m(latitude, longitude, target):
    target_latitude, target_longitude = target
    north_m = (target_latitude - float(latitude)) * 110570
    east_m = (
        (target_longitude - float(longitude))
        * 111320
        * np.cos(np.radians(float(latitude)))
    )
    return float(np.hypot(north_m, east_m))


def _raw_region_metadata(raw_payload):
    result = {}
    for region_key, data in (raw_payload.get("bolgeler") or {}).items():
        if isinstance(data, dict):
            result[region_key] = data
    return result


def _shortlist_rows(shortlist_payload):
    return [
        dict(item) for item in (shortlist_payload.get("kisa_liste") or [])
        if isinstance(item, dict)
    ]


def _analyze_region(region_key, rows, region_metadata):
    bbox = satellite.REGIONS[region_key]["bbox"]
    items = satellite._search_items(bbox)
    older = _find_item(items, region_metadata.get("onceki_item"))
    latest = _find_item(items, region_metadata.get("son_item"))
    if older is None or latest is None:
        return {
            "durum": "atlandi",
            "neden": "mikro_kaynak_sentinel_cifti_bulunamadi",
            "aday_sayisi": len(rows),
        }

    previous = _previous_scene(items, older, bbox)
    if previous is None:
        return {
            "durum": "atlandi",
            "neden": "degisim_oncesi_uygun_sentinel_sahnesi_bulunamadi",
            "aday_sayisi": len(rows),
            "onceki_item": older.get("id"),
            "son_item": latest.get("id"),
        }

    height, width = satellite._output_shape(bbox)
    previous_rgb, previous_ndvi, previous_brightness, previous_scl = _scene_arrays(
        previous, bbox, height, width
    )
    older_rgb, older_ndvi, older_brightness, older_scl = _scene_arrays(
        older, bbox, height, width
    )
    latest_rgb, latest_ndvi, latest_brightness, latest_scl = _scene_arrays(
        latest, bbox, height, width
    )

    valid = ~np.isin(previous_scl, satellite.EXCLUDED_SCL_CLASSES)
    valid &= ~np.isin(older_scl, satellite.EXCLUDED_SCL_CLASSES)
    valid &= ~np.isin(latest_scl, satellite.EXCLUDED_SCL_CLASSES)
    water = (previous_scl == 6) | (older_scl == 6) | (latest_scl == 6)
    valid &= ~satellite._dilate_mask(water, satellite.COASTAL_WATER_BUFFER_PIXELS)

    scene_previous = (previous_rgb, previous_ndvi, previous_brightness)
    scene_older = (older_rgb, older_ndvi, older_brightness)
    scene_latest = (latest_rgb, latest_ndvi, latest_brightness)

    analyzed = []
    for raw in rows:
        try:
            latitude = float(raw["enlem"])
            longitude = float(raw["boylam"])
        except (KeyError, TypeError, ValueError):
            continue
        row, column = _pixel_for_point(latitude, longitude, bbox, valid.shape)
        row_slice, col_slice = _patch_slices(row, column, valid.shape)
        metrics = _paired_metrics(
            scene_previous,
            scene_older,
            scene_latest,
            valid,
            row_slice,
            col_slice,
        )
        classification = _classify(metrics)
        item = dict(raw)
        if metrics:
            item.update(
                {
                    "uc_sahne_gecerli_oran": round(metrics["valid_fraction"], 3),
                    "onceki_rgb_degisim": round(metrics["previous_rgb_change"], 4),
                    "son_rgb_degisim": round(metrics["current_rgb_change"], 4),
                    "onceki_ndvi_kaybi": round(metrics["previous_ndvi_loss"], 4),
                    "son_ndvi_kaybi": round(metrics["current_ndvi_loss"], 4),
                    "onceki_parlaklik_artisi": round(metrics["previous_brightness_gain"], 4),
                    "son_parlaklik_artisi": round(metrics["current_brightness_gain"], 4),
                }
            )
        else:
            item["uc_sahne_gecerli_oran"] = 0.0
        item.update(
            {
                "onceki_donem_skoru": classification["previous_score"],
                "son_donem_skoru": classification["current_score"],
                "ani_baslangic_orani": classification["ratio"],
                "ani_baslangic_destegi": classification["abrupt"],
                "devam_eden_hareket_destegi": classification["continuing"],
                "onceki_zemin_hareketli_riski": classification["unstable"],
                "temporal_sinif": classification["label"],
                "gulbahce_cevre": _distance_m(
                    latitude, longitude, GULBAHCE_REFERENCE_POINT
                ) <= GULBAHCE_LABEL_RADIUS_M,
            }
        )
        analyzed.append(item)

    analyzed.sort(
        key=lambda item: (
            not bool(item.get("ani_baslangic_destegi")),
            not bool(item.get("devam_eden_hareket_destegi")),
            bool(item.get("onceki_zemin_hareketli_riski")),
            -_number(item.get("ani_baslangic_orani"), 0.0),
            -_number(item.get("son_donem_skoru"), 0.0),
        )
    )

    previous_time = _item_time(previous)
    older_time = _item_time(older)
    gap_days = (
        round((older_time - previous_time).total_seconds() / 86400, 1)
        if previous_time and older_time
        else None
    )
    return {
        "durum": "ok",
        "bolge": satellite.REGIONS[region_key]["label"],
        "degisim_oncesi_item": previous.get("id"),
        "degisim_oncesi_tarih": satellite._item_date(previous),
        "onceki_item": older.get("id"),
        "onceki_tarih": satellite._item_date(older),
        "son_item": latest.get("id"),
        "son_tarih": satellite._item_date(latest),
        "degisim_oncesi_aralik_gun": gap_days,
        "temporal_ornekleme": "ayni_3x3_yama_uc_sahne_ortak_gecerli_piksel",
        "olculen_aday": len(analyzed),
        "ani_baslangic_destegi": sum(
            bool(item.get("ani_baslangic_destegi")) for item in analyzed
        ),
        "devam_eden_hareket_destegi": sum(
            bool(item.get("devam_eden_hareket_destegi")) for item in analyzed
        ),
        "onceki_zemin_hareketli_riski": sum(
            bool(item.get("onceki_zemin_hareketli_riski")) for item in analyzed
        ),
        "gulbahce_olculen": sum(bool(item.get("gulbahce_cevre")) for item in analyzed),
        "adaylar": analyzed,
    }


def _self_check():
    assert satellite.MIN_HOTSPOT_AREA_M2 == 250

    abrupt_metrics = {
        "valid_fraction": 1.0,
        "previous_rgb_change": 0.05,
        "current_rgb_change": 0.28,
        "previous_ndvi_loss": 0.01,
        "current_ndvi_loss": 0.25,
        "previous_brightness_gain": 0.01,
        "current_brightness_gain": 0.08,
    }
    abrupt = _classify(abrupt_metrics)
    assert abrupt["abrupt"]
    assert abrupt["label"] == "ANI_BASLANGIC_DESTEGI"

    continuing_metrics = {
        "valid_fraction": 1.0,
        "previous_rgb_change": 0.24,
        "current_rgb_change": 0.28,
        "previous_ndvi_loss": 0.12,
        "current_ndvi_loss": 0.25,
        "previous_brightness_gain": 0.05,
        "current_brightness_gain": 0.08,
    }
    continuing = _classify(continuing_metrics)
    assert continuing["continuing"]
    assert continuing["label"] == "DEVAM_EDEN_HAREKET"

    low_valid = dict(abrupt_metrics)
    low_valid["valid_fraction"] = 5 / 9
    low = _classify(low_valid)
    assert not low["abrupt"]
    assert low["label"] == "YETERSIZ_GECERLI_VERI"


def run_review():
    _self_check()
    if not SHORTLIST_FILE.exists():
        raise RuntimeError("micro_site_shortlist.json bulunamadı.")
    if not RAW_AUDIT_FILE.exists():
        raise RuntimeError("micro_site_audit.json bulunamadı.")

    shortlist_payload = json.loads(SHORTLIST_FILE.read_text(encoding="utf-8"))
    raw_payload = json.loads(RAW_AUDIT_FILE.read_text(encoding="utf-8"))
    rows = _shortlist_rows(shortlist_payload)
    metadata = _raw_region_metadata(raw_payload)

    payload = {
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": satellite.MIN_HOTSPOT_AREA_M2,
        "mikro_aralik_m2": shortlist_payload.get("mikro_aralik_m2", [150, 249]),
        "kisa_liste_girdi": len(rows),
        "amac": (
            "Mikro adayın son Sentinel değişiminin, aynı 3x3 yamadaki bir önceki "
            "Sentinel dönemine göre ani başlangıç mı, devam eden hareket mi yoksa "
            "tek-dönem sinyali mi olduğunu diagnostik olarak ölçmek."
        ),
        "esikler": {
            "patch_yaricap_piksel": PATCH_RADIUS_PIXELS,
            "minimum_gecerli_oran": MIN_VALID_FRACTION,
            "minimum_onceki_sahne_araligi_gun": MIN_PREVIOUS_GAP_DAYS,
            "ani_baslangic_min_oran": ABRUPT_RATIO_MIN,
            "ani_baslangic_onceki_goreli_tavan": ABRUPT_PREVIOUS_RELATIVE_MAX,
            "devam_onceki_skor_min": CONTINUING_PREVIOUS_SCORE_MIN,
            "devam_son_skor_min": CONTINUING_CURRENT_SCORE_MIN,
        },
        "uyari": (
            "Bu çıktı alarm/görev üretmez ve imar/parsel doğrulaması değildir. "
            "Temporal destek tek başına gerçek şantiye kabul edilmez; saha ve/veya "
            "güvenilir açık kaynak doğrulaması gerekir."
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
                "onceki_zemin_hareketli_riski": 0,
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
            payload["bolgeler"][region_key] = _analyze_region(
                region_key, region_rows, region_metadata
            )
        except Exception as exc:
            payload["bolgeler"][region_key] = {
                "durum": "hata",
                "neden": f"{type(exc).__name__}: {exc}",
                "aday_sayisi": len(region_rows),
            }

    payload["toplam"] = {
        "olculen_aday": sum(
            int(data.get("olculen_aday") or 0)
            for data in payload["bolgeler"].values()
            if isinstance(data, dict)
        ),
        "ani_baslangic_destegi": sum(
            int(data.get("ani_baslangic_destegi") or 0)
            for data in payload["bolgeler"].values()
            if isinstance(data, dict)
        ),
        "devam_eden_hareket_destegi": sum(
            int(data.get("devam_eden_hareket_destegi") or 0)
            for data in payload["bolgeler"].values()
            if isinstance(data, dict)
        ),
        "onceki_zemin_hareketli_riski": sum(
            int(data.get("onceki_zemin_hareketli_riski") or 0)
            for data in payload["bolgeler"].values()
            if isinstance(data, dict)
        ),
        "gulbahce_olculen": sum(
            int(data.get("gulbahce_olculen") or 0)
            for data in payload["bolgeler"].values()
            if isinstance(data, dict)
        ),
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
        print("Mikro zaman-serisi öz testi başarılı; alarm/görev/eşik değişmedi.")
        return

    payload = run_review()
    total = payload.get("toplam") or {}
    print(
        "Mikro zaman-serisi incelemesi tamamlandı: "
        f"ölçülen={int(total.get('olculen_aday') or 0)}, "
        f"ani={int(total.get('ani_baslangic_destegi') or 0)}, "
        f"devam={int(total.get('devam_eden_hareket_destegi') or 0)}, "
        f"önceki-hareketli={int(total.get('onceki_zemin_hareketli_riski') or 0)}, "
        f"Gülbahçe={int(total.get('gulbahce_olculen') or 0)}. "
        "Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
