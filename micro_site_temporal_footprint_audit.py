"""Mikro şantiye temporal kanıtında 3x3 yama ile gerçek bileşen izini karşılaştırır.

Bu katman yalnız diagnostiktir. Ana Sentinel üretim eşiği 250 m² olarak kalır,
150-249 m² mikro adaylardan alarm veya saha görevi üretmez ve mevcut karar
sınıflarını değiştirmez.

Amaç, iki piksellik kompakt bir mikro adayın temporal skorunun çevredeki 3x3
yamadaki tarla/toprak hareketinden yanlışlıkla güçlenip güçlenmediğini veya tam
tersine gerçek lokal sinyalin 3x3 ortalamada seyrelip seyrelmediğini ölçmektir.
Böylece eşik değiştirmeden önce örnekleme geometrisinin etkisi sayısallaştırılır.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

import micro_site_audit
import micro_site_temporal_guard as temporal
import satellite


SHORTLIST_FILE = Path(__file__).with_name("micro_site_shortlist.json")
RAW_AUDIT_FILE = Path(__file__).with_name("micro_site_audit.json")
TEMPORAL_FILE = Path(__file__).with_name("micro_site_temporal_review.json")
OUTPUT_FILE = Path(__file__).with_name("micro_site_temporal_footprint_audit.json")
ISTANBUL = ZoneInfo("Europe/Istanbul")



def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default



def _flatten_temporal(payload):
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



def _distance_m(first, second):
    lat1 = _number(first.get("enlem"), None)
    lon1 = _number(first.get("boylam"), None)
    lat2 = _number(second.get("enlem"), None)
    lon2 = _number(second.get("boylam"), None)
    if None in (lat1, lon1, lat2, lon2):
        return float("inf")
    mean_lat = (lat1 + lat2) / 2
    north_m = (lat2 - lat1) * 110570
    east_m = (lon2 - lon1) * 111320 * np.cos(np.radians(mean_lat))
    return float(np.hypot(north_m, east_m))



def _temporal_match(candidate, rows):
    same_region = [row for row in rows if row.get("bolge") == candidate.get("bolge")]
    if not same_region:
        return None
    best = min(same_region, key=lambda row: _distance_m(candidate, row))
    return best if _distance_m(candidate, best) <= 20 else None



def _component_for_point(strict, row, column):
    """Temsil pikselini içeren mikro bağlı bileşeni bul; 1 piksel tolerans yalnız tanı içindir."""
    components = [np.asarray(component, dtype="int32") for component in satellite._connected_components(strict)]
    for component in components:
        if np.any((component[:, 0] == row) & (component[:, 1] == column)):
            return component, 0.0

    best = None
    best_distance = float("inf")
    for component in components:
        distances = np.hypot(component[:, 0] - row, component[:, 1] - column)
        distance = float(np.min(distances)) if len(distances) else float("inf")
        if distance < best_distance:
            best = component
            best_distance = distance
    if best is not None and best_distance <= 1.01:
        return best, best_distance
    return None, None



def _paired_component_metrics(previous, older, latest, valid_mask, component):
    previous_rgb, previous_ndvi, previous_brightness = previous
    older_rgb, older_ndvi, older_brightness = older
    latest_rgb, latest_ndvi, latest_brightness = latest

    rows = component[:, 0]
    columns = component[:, 1]
    point_valid = valid_mask[rows, columns]
    total = int(len(component))
    valid_count = int(point_valid.sum())
    valid_fraction = valid_count / max(total, 1)
    if not valid_count:
        return None

    rows = rows[point_valid]
    columns = columns[point_valid]
    prev_rgb_delta = np.mean(np.abs(older_rgb - previous_rgb), axis=2)[rows, columns]
    curr_rgb_delta = np.mean(np.abs(latest_rgb - older_rgb), axis=2)[rows, columns]
    prev_ndvi_loss = (previous_ndvi - older_ndvi)[rows, columns]
    curr_ndvi_loss = (older_ndvi - latest_ndvi)[rows, columns]
    prev_brightness_gain = (older_brightness - previous_brightness)[rows, columns]
    curr_brightness_gain = (latest_brightness - older_brightness)[rows, columns]

    return {
        "valid_fraction": valid_fraction,
        "previous_rgb_change": float(np.mean(prev_rgb_delta)),
        "current_rgb_change": float(np.mean(curr_rgb_delta)),
        "previous_ndvi_loss": float(np.mean(prev_ndvi_loss)),
        "current_ndvi_loss": float(np.mean(curr_ndvi_loss)),
        "previous_brightness_gain": float(np.mean(prev_brightness_gain)),
        "current_brightness_gain": float(np.mean(curr_brightness_gain)),
    }



def _region_context(region_key, region_metadata):
    bbox = satellite.REGIONS[region_key]["bbox"]
    items = satellite._search_items(bbox)
    older = temporal._find_item(items, region_metadata.get("onceki_item"))
    latest = temporal._find_item(items, region_metadata.get("son_item"))
    if older is None or latest is None:
        return None, "mikro_kaynak_sentinel_cifti_bulunamadi"
    previous = temporal._previous_scene(items, older, bbox)
    if previous is None:
        return None, "degisim_oncesi_uygun_sentinel_sahnesi_bulunamadi"

    height, width = satellite._output_shape(bbox)
    strict, _, _, _ = micro_site_audit._strict_micro_mask(older, latest, bbox, height, width)
    previous_rgb, previous_ndvi, previous_brightness, previous_scl = temporal._scene_arrays(
        previous, bbox, height, width
    )
    older_rgb, older_ndvi, older_brightness, older_scl = temporal._scene_arrays(
        older, bbox, height, width
    )
    latest_rgb, latest_ndvi, latest_brightness, latest_scl = temporal._scene_arrays(
        latest, bbox, height, width
    )

    valid = ~np.isin(previous_scl, satellite.EXCLUDED_SCL_CLASSES)
    valid &= ~np.isin(older_scl, satellite.EXCLUDED_SCL_CLASSES)
    valid &= ~np.isin(latest_scl, satellite.EXCLUDED_SCL_CLASSES)
    water = (previous_scl == 6) | (older_scl == 6) | (latest_scl == 6)
    valid &= ~satellite._dilate_mask(water, satellite.COASTAL_WATER_BUFFER_PIXELS)

    return {
        "bbox": bbox,
        "strict": strict,
        "valid": valid,
        "previous": (previous_rgb, previous_ndvi, previous_brightness),
        "older": (older_rgb, older_ndvi, older_brightness),
        "latest": (latest_rgb, latest_ndvi, latest_brightness),
        "previous_item": previous.get("id"),
        "older_item": older.get("id"),
        "latest_item": latest.get("id"),
        "shape": valid.shape,
    }, None



def _analyze_candidate(candidate, temporal_row, context):
    latitude = float(candidate["enlem"])
    longitude = float(candidate["boylam"])
    row, column = temporal._pixel_for_point(latitude, longitude, context["bbox"], context["shape"])
    component, pixel_distance = _component_for_point(context["strict"], row, column)

    result = dict(candidate)
    result.update(
        {
            "3x3_temporal_sinif": temporal_row.get("temporal_sinif") if temporal_row else None,
            "3x3_ani_baslangic": bool(temporal_row.get("ani_baslangic_destegi")) if temporal_row else False,
            "3x3_devam_eden": bool(temporal_row.get("devam_eden_hareket_destegi")) if temporal_row else False,
            "bilesen_eslesti": component is not None,
            "bilesen_esleme_piksel_mesafesi": round(pixel_distance, 2) if pixel_distance is not None else None,
        }
    )
    if component is None:
        result.update(
            {
                "bilesen_temporal_sinif": "BILESEN_BULUNAMADI",
                "bilesen_ani_baslangic": False,
                "bilesen_devam_eden": False,
                "sinif_farki": True,
                "ornekleme_riski": "bilesen_esleme_yok",
            }
        )
        return result

    metrics = _paired_component_metrics(
        context["previous"], context["older"], context["latest"], context["valid"], component
    )
    classification = temporal._classify(metrics)
    three_class = result["3x3_temporal_sinif"]
    footprint_class = classification["label"]
    three_abrupt = result["3x3_ani_baslangic"]
    footprint_abrupt = bool(classification["abrupt"])
    three_continuing = result["3x3_devam_eden"]
    footprint_continuing = bool(classification["continuing"])

    if three_abrupt and not footprint_abrupt:
        risk = "3x3_cevre_sinyali_olasi"
    elif footprint_abrupt and not three_abrupt:
        risk = "3x3_lokal_sinyali_seyreltmis_olabilir"
    elif three_continuing != footprint_continuing:
        risk = "devam_sinifi_ornekleme_duyarli"
    else:
        risk = "uyumlu"

    result.update(
        {
            "bilesen_piksel": int(len(component)),
            "bilesen_gecerli_oran": round(_number(metrics.get("valid_fraction") if metrics else 0), 3),
            "bilesen_onceki_skor": classification["previous_score"],
            "bilesen_son_skor": classification["current_score"],
            "bilesen_ani_baslangic_orani": classification["ratio"],
            "bilesen_temporal_sinif": footprint_class,
            "bilesen_ani_baslangic": footprint_abrupt,
            "bilesen_devam_eden": footprint_continuing,
            "bilesen_onceki_zemin_hareketli_riski": bool(classification["unstable"]),
            "sinif_farki": bool(three_class != footprint_class),
            "ani_destek_farki": bool(three_abrupt != footprint_abrupt),
            "devam_destek_farki": bool(three_continuing != footprint_continuing),
            "ornekleme_riski": risk,
        }
    )
    return result



def build_audit():
    if not SHORTLIST_FILE.exists() or not RAW_AUDIT_FILE.exists() or not TEMPORAL_FILE.exists():
        raise RuntimeError("Mikro kısa liste / ham audit / temporal review dosyalarından biri eksik.")

    shortlist = json.loads(SHORTLIST_FILE.read_text(encoding="utf-8"))
    raw_audit = json.loads(RAW_AUDIT_FILE.read_text(encoding="utf-8"))
    temporal_payload = json.loads(TEMPORAL_FILE.read_text(encoding="utf-8"))
    candidates = [dict(row) for row in (shortlist.get("kisa_liste") or []) if isinstance(row, dict)]
    temporal_rows = _flatten_temporal(temporal_payload)
    region_metadata = temporal._raw_region_metadata(raw_audit)

    payload = {
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": satellite.MIN_HOTSPOT_AREA_M2,
        "mikro_aralik_m2": shortlist.get("mikro_aralik_m2", [150, 249]),
        "amac": "Mevcut 3x3 mikro temporal örneklemeyi adayın gerçek iki-piksel bağlı bileşen iziyle karşılaştırmak.",
        "uyari": "Bu denetim mevcut alarm/görev/karar sınıfını değiştirmez; yalnız örnekleme geometrisi riskini ölçer.",
        "bolgeler": {},
    }

    all_rows = []
    for region_key in ("cesme", "uzunkuyu"):
        rows = [row for row in candidates if row.get("bolge") == region_key]
        if not rows:
            payload["bolgeler"][region_key] = {"durum": "ok", "olculen": 0, "adaylar": []}
            continue
        metadata = region_metadata.get(region_key) or {}
        if metadata.get("durum") != "ok":
            payload["bolgeler"][region_key] = {
                "durum": "atlandi", "neden": "mikro_kaynak_bolge_ok_degil", "aday_sayisi": len(rows)
            }
            continue
        context, error = _region_context(region_key, metadata)
        if error:
            payload["bolgeler"][region_key] = {
                "durum": "atlandi", "neden": error, "aday_sayisi": len(rows)
            }
            continue
        analyzed = []
        for candidate in rows:
            temporal_row = _temporal_match(candidate, temporal_rows)
            analyzed.append(_analyze_candidate(candidate, temporal_row, context))
        all_rows.extend(analyzed)
        payload["bolgeler"][region_key] = {
            "durum": "ok",
            "olculen": len(analyzed),
            "sinif_farki": sum(bool(row.get("sinif_farki")) for row in analyzed),
            "ani_destek_farki": sum(bool(row.get("ani_destek_farki")) for row in analyzed),
            "devam_destek_farki": sum(bool(row.get("devam_destek_farki")) for row in analyzed),
            "adaylar": analyzed,
        }

    payload["toplam"] = {
        "olculen": len(all_rows),
        "bilesen_eslesmeyen": sum(not bool(row.get("bilesen_eslesti")) for row in all_rows),
        "sinif_farki": sum(bool(row.get("sinif_farki")) for row in all_rows),
        "ani_destek_farki": sum(bool(row.get("ani_destek_farki")) for row in all_rows),
        "devam_destek_farki": sum(bool(row.get("devam_destek_farki")) for row in all_rows),
        "3x3_cevre_sinyali_olasi": sum(row.get("ornekleme_riski") == "3x3_cevre_sinyali_olasi" for row in all_rows),
        "3x3_lokal_sinyali_seyreltmis_olabilir": sum(row.get("ornekleme_riski") == "3x3_lokal_sinyali_seyreltmis_olabilir" for row in all_rows),
    }
    return payload



def _self_check():
    assert satellite.MIN_HOTSPOT_AREA_M2 == 250
    strict = np.zeros((5, 5), dtype=bool)
    strict[2, 2] = True
    strict[2, 3] = True
    component, distance = _component_for_point(strict, 2, 2)
    assert component is not None and len(component) == 2 and distance == 0.0
    component, distance = _component_for_point(strict, 1, 2)
    assert component is not None and distance == 1.0



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Mikro bileşen-izi temporal audit öz testi başarılı; eşik/alarm/görev değişmedi.")
        return

    payload = build_audit()
    OUTPUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total = payload.get("toplam") or {}
    print(
        "Mikro temporal örnekleme audit tamamlandı: "
        f"ölçülen={int(total.get('olculen') or 0)}, "
        f"sınıf_farkı={int(total.get('sinif_farki') or 0)}, "
        f"ani_farkı={int(total.get('ani_destek_farki') or 0)}, "
        f"çevre_riski={int(total.get('3x3_cevre_sinyali_olasi') or 0)}, "
        f"seyrelme_riski={int(total.get('3x3_lokal_sinyali_seyreltmis_olabilir') or 0)}."
    )


if __name__ == "__main__":
    main()
