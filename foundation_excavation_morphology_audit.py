"""Temel/kepçe kazısı ile tarla düzeltmesini morfolojik olarak ayıran diagnostik.

Bu katman YALNIZCA denetim üretir. Alarm, saha görevi veya rota kararı üretmez;
250 m² ana eşiği ile 150–249 m² MİKRO politikasını değiştirmez.

Sentinel-2 10 m veriden gerçek kazı derinliği ölçülemez. Buradaki
``temel_kazi_proxy_puani``; kompakt/kapalı geometri, çevresel yerellik,
kuvvetli çekirdek ve tarımsal geniş-yüzey bağlamını birleştirerek yalnızca
"temel/derin kazı olasılığı" için morfolojik bir proxy üretir.

Saha kalibrasyonu özellikle 17 Eylül yanlış-pozitiflerini ve 16 Eylül gerçek
kazı yanlış-negatifini karşılaştırır. Bu ilk sürüm üretim kapısı değildir;
regresyon davranışı ölçülmeden operasyon rotasına bağlanmamalıdır.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

import satellite


OUTPUT_JSON = Path(__file__).with_name("foundation_excavation_morphology_review.json")
FEEDBACK_JSON = Path(__file__).with_name("manual_field_feedback.json")

MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
MATCH_DEFAULT_M = 30.0
EXAMPLE_LIMIT = 12

HIGH_SCORE = 65
MEDIUM_SCORE = 45


def _perimeter_edges(component):
    pixels = set(component)
    exposed = 0
    for row, column in component:
        exposed += (row - 1, column) not in pixels
        exposed += (row + 1, column) not in pixels
        exposed += (row, column - 1) not in pixels
        exposed += (row, column + 1) not in pixels
    return max(int(exposed), 1)


def _component_mask(shape, component):
    mask = np.zeros(shape, dtype=bool)
    if component:
        pixels = np.asarray(component, dtype="int32")
        mask[pixels[:, 0], pixels[:, 1]] = True
    return mask


def _shape_features(component):
    pixels = np.asarray(component, dtype="int32")
    rows = pixels[:, 0]
    columns = pixels[:, 1]
    row_span = int(rows.max() - rows.min() + 1)
    col_span = int(columns.max() - columns.min() + 1)
    short_span = max(min(row_span, col_span), 1)
    long_span = max(row_span, col_span)
    aspect = float(long_span / short_span)
    fill = float(len(component) / max(row_span * col_span, 1))
    perimeter = _perimeter_edges(component)
    compactness = float(4 * math.pi * len(component) / (perimeter * perimeter))
    return {
        "satir_span": row_span,
        "sutun_span": col_span,
        "uzun_kisa_orani": round(aspect, 3),
        "kutu_doluluk_orani": round(fill, 3),
        "kompaktlik": round(compactness, 3),
    }


def _component_location(component, bbox, shape):
    pixels = np.asarray(component, dtype="int32")
    centroid = pixels.mean(axis=0)
    distance = np.sum((pixels - centroid) ** 2, axis=1)
    representative = pixels[int(np.argmin(distance))]
    row, column = int(representative[0]), int(representative[1])
    height, width = shape
    west, south, east, north = bbox
    latitude = north - (row + 0.5) / height * (north - south)
    longitude = west + (column + 0.5) / width * (east - west)
    return round(latitude, 6), round(longitude, 6)


def _fraction(mask, selector):
    count = int(np.count_nonzero(selector))
    if count <= 0:
        return 0.0
    return float(np.mean(mask[selector]))


def _agricultural_context(component, context_mask, pixel_area_m2):
    if context_mask is None:
        return 0.0, 0.0, False
    labels = np.zeros(context_mask.shape, dtype="int32")
    sizes = {}
    for label, context_component in enumerate(
        satellite._connected_components(context_mask),
        start=1,
    ):
        pixels = np.asarray(context_component, dtype="int32")
        labels[pixels[:, 0], pixels[:, 1]] = label
        sizes[label] = len(context_component)

    pixels = np.asarray(component, dtype="int32")
    overlap = labels[pixels[:, 0], pixels[:, 1]]
    active, counts = np.unique(overlap[overlap > 0], return_counts=True)
    context_area = 0.0
    if active.size:
        best = int(active[int(np.argmax(counts))])
        context_area = sizes.get(best, 0) * pixel_area_m2

    area_m2 = len(component) * pixel_area_m2
    ratio = context_area / max(area_m2, pixel_area_m2)
    risk = bool(
        area_m2 <= satellite.AGRICULTURAL_CONTEXT_MAX_CORE_M2
        and context_area >= satellite.AGRICULTURAL_CONTEXT_MIN_AREA_M2
        and ratio >= satellite.AGRICULTURAL_CONTEXT_MIN_RATIO
    )
    return float(context_area), float(ratio), risk


def _score_component(
    component,
    change_mask,
    small_site_mask,
    agricultural_context_mask,
    bbox,
    pixel_area_m2,
):
    area_m2 = len(component) * pixel_area_m2
    features = _shape_features(component)
    aspect = float(features["uzun_kisa_orani"])
    fill = float(features["kutu_doluluk_orani"])
    compactness = float(features["kompaktlik"])

    component_mask = _component_mask(change_mask.shape, component)
    inner_dilated = satellite._dilate_mask(component_mask, 2)
    outer_dilated = satellite._dilate_mask(component_mask, 5)
    inner_ring = inner_dilated & ~component_mask
    outer_ring = outer_dilated & ~inner_dilated

    # İki bağlamı kesin olarak ayır: değişim maskesi hafriyat/ikincil bozulma
    # proxy'sidir; agricultural_context_mask ise yalnız tarla/bahçe negatif
    # bağlamıdır. Tarım maskesini pozitif "spoil" kanıtı gibi ödüllendirmek
    # tarla yanlış-pozitiflerini yapay olarak yükseltir.
    change_context_mask = np.asarray(change_mask, dtype=bool)
    agricultural_mask = (
        np.asarray(agricultural_context_mask, dtype=bool)
        if agricultural_context_mask is not None
        else np.zeros(change_mask.shape, dtype=bool)
    )
    strong_mask = (
        np.asarray(small_site_mask, dtype=bool)
        if small_site_mask is not None
        else np.zeros(change_mask.shape, dtype=bool)
    )
    pixels = np.asarray(component, dtype="int32")
    strong_fraction = float(np.mean(strong_mask[pixels[:, 0], pixels[:, 1]]))
    inner_change_fraction = _fraction(change_context_mask, inner_ring)
    outer_change_fraction = _fraction(change_context_mask, outer_ring)
    inner_agricultural_fraction = _fraction(agricultural_mask, inner_ring)
    outer_agricultural_fraction = _fraction(agricultural_mask, outer_ring)
    context_area_m2, context_ratio, agricultural_risk = _agricultural_context(
        component,
        agricultural_context_mask,
        pixel_area_m2,
    )

    score = 0
    reasons = []
    penalties = []

    if 250 <= area_m2 <= 2500:
        score += 15
        reasons.append("parsel/temel ölçeğine yakın alan")
    elif area_m2 <= 5000:
        score += 7
        reasons.append("orta parsel ölçeği")

    if fill >= 0.45:
        score += 15
        reasons.append("kapalı/dikdörtgensel doluluk güçlü")
    elif fill >= 0.30:
        score += 8
        reasons.append("kapalı geometri orta")

    if aspect <= 3.0:
        score += 15
        reasons.append("uzun-şerit değil")
    elif aspect <= 4.5:
        score += 7

    if compactness >= 0.18:
        score += 10
        reasons.append("kompakt çekirdek")
    elif compactness >= 0.14:
        score += 5

    if strong_fraction >= 0.50:
        score += 15
        reasons.append("güçlü değişim çekirdeği")
    elif strong_fraction >= 0.25:
        score += 8

    # Temel kazısında çekirdeğin hemen yanında ayrı hafriyat/ikincil bozulma
    # zonu olabilir. Bu kanıt yalnız gerçek değişim maskesinden gelir.
    if 0.05 <= inner_change_fraction <= 0.45:
        score += 10
        reasons.append("çekirdek yanında sınırlı ikincil yüzey değişimi")
    if outer_change_fraction <= 0.15:
        score += 10
        reasons.append("dış çevre büyük ölçüde stabil")

    if agricultural_risk:
        score -= 50
        penalties.append("geniş tarımsal/çıplak-zemin bağlamı")
    if aspect >= 5.0:
        score -= 20
        penalties.append("uzun-şerit geometri")
    if compactness <= 0.12:
        score -= 15
        penalties.append("düşük kompaktlık")
    if outer_change_fraction >= 0.50:
        score -= 20
        penalties.append("geniş homojen çevresel değişim")
    if area_m2 > 10000:
        score -= 10
        penalties.append("çok geniş yüzey hareketi")

    score = max(0, min(100, int(round(score))))
    if score >= HIGH_SCORE:
        level = "YUKSEK"
    elif score >= MEDIUM_SCORE:
        level = "ORTA"
    else:
        level = "DUSUK"

    latitude, longitude = _component_location(component, bbox, change_mask.shape)
    return {
        "enlem": latitude,
        "boylam": longitude,
        "alan_m2": int(round(area_m2)),
        **features,
        "guclu_cekirdek_orani": round(strong_fraction, 3),
        "yakin_cevre_degisim_orani": round(inner_change_fraction, 3),
        "dis_cevre_degisim_orani": round(outer_change_fraction, 3),
        "yakin_cevre_tarim_orani": round(inner_agricultural_fraction, 3),
        "dis_cevre_tarim_orani": round(outer_agricultural_fraction, 3),
        "tarim_baglam_alani_m2": int(round(context_area_m2)),
        "tarim_baglam_orani": round(context_ratio, 3),
        "tarim_riski": agricultural_risk,
        "temel_kazi_proxy_puani": score,
        "temel_kazi_proxy_seviyesi": level,
        "pozitif_kanitlar": reasons,
        "negatif_kanitlar": penalties,
        "uyari": "Bu puan gerçek kazı derinliği değildir; Sentinel-2 morfoloji proxy'sidir.",
    }


def _eligible(component, pixel_area_m2, small_site_mask):
    area_m2 = len(component) * pixel_area_m2
    if area_m2 < satellite.MIN_HOTSPOT_AREA_M2:
        return False
    if area_m2 > satellite.SMALL_HOTSPOT_MAX_M2:
        return True
    if small_site_mask is None:
        return False
    pixels = np.asarray(component, dtype="int32")
    strong_fraction = float(np.mean(small_site_mask[pixels[:, 0], pixels[:, 1]]))
    return strong_fraction >= 0.50


def _distance_m(a, b):
    lat1, lon1 = float(a[0]), float(a[1])
    lat2, lon2 = float(b[0]), float(b[1])
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _load_feedback():
    try:
        payload = json.loads(FEEDBACK_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    return [x for x in payload.get("kayitlar", []) if isinstance(x, dict)]


def _relevant_feedback(region_key):
    bbox = satellite.REGIONS[region_key]["bbox"]
    west, south, east, north = bbox
    results = []
    for item in _load_feedback():
        result = str(item.get("sonuc") or "").upper()
        if result not in {"YANLIS_POZITIF", "DOGRULANMIS_KAZI"}:
            continue
        try:
            lat = float(item["enlem"])
            lon = float(item["boylam"])
        except (KeyError, TypeError, ValueError):
            continue
        if south <= lat <= north and west <= lon <= east:
            results.append(item)
    return results


def _match_feedback(feedback, records):
    output = []
    for item in feedback:
        target = (float(item["enlem"]), float(item["boylam"]))
        radius = float(item.get("eslesme_yaricapi_m") or MATCH_DEFAULT_M)
        nearest = None
        nearest_distance = None
        for record in records:
            distance = _distance_m(target, (record["enlem"], record["boylam"]))
            if nearest_distance is None or distance < nearest_distance:
                nearest = record
                nearest_distance = distance
        matched = nearest is not None and nearest_distance is not None and nearest_distance <= radius
        expected = str(item.get("sonuc") or "").upper()
        if matched:
            level = str(nearest.get("temel_kazi_proxy_seviyesi") or "")
            if expected == "YANLIS_POZITIF":
                regression_ok = level != "YUKSEK"
            else:
                regression_ok = level in {"ORTA", "YUKSEK"}
        else:
            regression_ok = expected == "YANLIS_POZITIF"
        output.append(
            {
                "id": item.get("id"),
                "sonuc": expected,
                "enlem": round(target[0], 6),
                "boylam": round(target[1], 6),
                "ham_maske_yakaladi": bool(matched),
                "en_yakin_mesafe_m": round(nearest_distance, 1) if nearest_distance is not None else None,
                "proxy_puani": nearest.get("temel_kazi_proxy_puani") if matched else None,
                "proxy_seviyesi": nearest.get("temel_kazi_proxy_seviyesi") if matched else None,
                "regresyon_uyumlu": bool(regression_ok),
            }
        )
    return output


def _analyze_region(region_key):
    captured = {}
    original = satellite._hotspots

    def capture_hotspots(
        change_mask,
        bbox,
        pixel_area_m2,
        small_site_mask=None,
        agricultural_context_mask=None,
        limit=satellite.HOTSPOT_LIMIT,
        small_quota=satellite.SMALL_HOTSPOT_QUOTA,
    ):
        captured["change_mask"] = np.asarray(change_mask, dtype=bool).copy()
        captured["bbox"] = list(bbox)
        captured["pixel_area_m2"] = float(pixel_area_m2)
        captured["small_site_mask"] = (
            np.asarray(small_site_mask, dtype=bool).copy()
            if small_site_mask is not None
            else None
        )
        captured["agricultural_context_mask"] = (
            np.asarray(agricultural_context_mask, dtype=bool).copy()
            if agricultural_context_mask is not None
            else None
        )
        return original(
            change_mask,
            bbox,
            pixel_area_m2,
            small_site_mask=small_site_mask,
            agricultural_context_mask=agricultural_context_mask,
            limit=limit,
            small_quota=small_quota,
        )

    pair = satellite.sentinel_pair(region_key)
    satellite._hotspots = capture_hotspots
    try:
        result = satellite.analyze_sentinel_change(region_key, pair=pair)
    finally:
        satellite._hotspots = original

    change_mask = captured["change_mask"]
    small_mask = captured.get("small_site_mask")
    agricultural_mask = captured.get("agricultural_context_mask")
    bbox = captured["bbox"]
    pixel_area = captured["pixel_area_m2"]

    records = []
    for component in satellite._connected_components(change_mask):
        if not _eligible(component, pixel_area, small_mask):
            continue
        records.append(
            _score_component(
                component,
                change_mask,
                small_mask,
                agricultural_mask,
                bbox,
                pixel_area,
            )
        )
    records.sort(
        key=lambda x: (x["temel_kazi_proxy_puani"], -x["alan_m2"]),
        reverse=True,
    )

    feedback = _match_feedback(_relevant_feedback(region_key), records)
    regression_failures = [x for x in feedback if not x["regresyon_uyumlu"]]
    high = [x for x in records if x["temel_kazi_proxy_seviyesi"] == "YUKSEK"]

    return {
        "bolge": satellite.REGIONS[region_key]["label"],
        "durum": "ok",
        "onceki_tarih": result.get("older_date"),
        "son_tarih": result.get("latest_date"),
        "older_item": result.get("older_item"),
        "latest_item": result.get("latest_item"),
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "alarm": False,
        "saha_gorevi": False,
        "ham_morfoloji_adayi": len(records),
        "yuksek_proxy_adayi": len(high),
        "en_yuksek_ornekler": records[:EXAMPLE_LIMIT],
        "saha_kalibrasyonu": feedback,
        "regresyon_uyumsuz_sayisi": len(regression_failures),
        "regresyon_uyumsuz_ornekler": regression_failures,
    }


def _self_check():
    square = [(r, c) for r in range(4, 8) for c in range(4, 8)]
    strip = [(r, c) for r in range(4, 6) for c in range(2, 14)]
    shape = (20, 20)
    change = np.zeros(shape, dtype=bool)
    small = np.zeros(shape, dtype=bool)
    agriculture = np.zeros(shape, dtype=bool)
    for r, c in square:
        change[r, c] = True
        small[r, c] = True
    square_score = _score_component(
        square, change, small, agriculture, [26.3, 38.2, 26.32, 38.22], 100.0
    )
    assert square_score["temel_kazi_proxy_puani"] >= MEDIUM_SCORE, square_score
    assert square_score["yakin_cevre_degisim_orani"] == 0.0, square_score
    assert "çekirdek yanında sınırlı ikincil yüzey değişimi" not in square_score["pozitif_kanitlar"], square_score

    # Tarım/bahçe bağlamı, çekirdeğin yanında olsa bile gerçek değişim yoksa
    # ikincil hafriyat/spoil pozitif kanıtı üretemez.
    agriculture_ring = satellite._dilate_mask(_component_mask(shape, square), 2)
    agriculture_ring &= ~_component_mask(shape, square)
    agri_ring_score = _score_component(
        square,
        change,
        small,
        agriculture_ring,
        [26.3, 38.2, 26.32, 38.22],
        100.0,
    )
    assert agri_ring_score["yakin_cevre_tarim_orani"] > 0.0, agri_ring_score
    assert agri_ring_score["yakin_cevre_degisim_orani"] == 0.0, agri_ring_score
    assert "çekirdek yanında sınırlı ikincil yüzey değişimi" not in agri_ring_score["pozitif_kanitlar"], agri_ring_score

    # Ayrı ve gerçekten değişmiş yakın zon varsa pozitif ikincil-bozulma kanıtı
    # üretilebildiğini de doğrula. Bir piksel boşluk bırakıldığı için ana çekirdekle
    # tek bağlı bileşene birleşmez.
    change_with_spoil = change.copy()
    for c in range(4, 8):
        change_with_spoil[2, c] = True
    spoil_score = _score_component(
        square,
        change_with_spoil,
        small,
        agriculture,
        [26.3, 38.2, 26.32, 38.22],
        100.0,
    )
    assert spoil_score["yakin_cevre_degisim_orani"] > 0.0, spoil_score
    assert "çekirdek yanında sınırlı ikincil yüzey değişimi" in spoil_score["pozitif_kanitlar"], spoil_score

    change2 = np.zeros(shape, dtype=bool)
    small2 = np.zeros(shape, dtype=bool)
    agriculture2 = np.ones(shape, dtype=bool)
    for r, c in strip:
        change2[r, c] = True
        small2[r, c] = True
    strip_score = _score_component(
        strip, change2, small2, agriculture2, [26.3, 38.2, 26.32, 38.22], 100.0
    )
    assert strip_score["temel_kazi_proxy_puani"] < square_score["temel_kazi_proxy_puani"], (
        square_score,
        strip_score,
    )
    assert strip_score["temel_kazi_proxy_seviyesi"] != "YUKSEK", strip_score


def audit():
    _self_check()
    regions = {}
    for region_key in ("cesme", "uzunkuyu"):
        try:
            regions[region_key] = _analyze_region(region_key)
        except Exception as exc:
            regions[region_key] = {
                "bolge": satellite.REGIONS[region_key]["label"],
                "durum": "hata",
                "hata": f"{type(exc).__name__}: {exc}",
            }

    failures = []
    for region_key, region in regions.items():
        for item in region.get("regresyon_uyumsuz_ornekler", []) if isinstance(region, dict) else []:
            failures.append({"bolge_anahtari": region_key, **item})

    return {
        "surum": 1,
        "amac": "Temel/kepçe kazısı olasılığı için Sentinel-2 morfoloji proxy diagnostigi",
        "gercek_derinlik_olcumu": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "alarm": False,
        "saha_gorevi": False,
        "proxy_esikleri": {"yuksek": HIGH_SCORE, "orta": MEDIUM_SCORE},
        "bolgeler": regions,
        "toplam_regresyon_uyumsuz": len(failures),
        "regresyon_uyumsuzluklari": failures,
        "not": (
            "İlk sürüm yalnız diagnostiktir. Tek Sentinel-2 spektral/toprak değişimi "
            "operasyon görevi üretmez. Saha kalibrasyonu yeterince çoğalmadan bu puan "
            "rota kapısına bağlanmamalıdır."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("foundation excavation morphology self-check: ok")
        return
    payload = audit()
    OUTPUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
