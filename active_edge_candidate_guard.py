"""Aktif ana Sentinel adaylarında analiz-kutusu kenarı kırpma riskini ölçer.

Bu katman alarm veya saha görevi üretmez ve 250 m² ana üretim eşiğini değiştirmez.
Amaç, güncel Sentinel sahnesindeki bir üretim bileşeni analiz bbox sınırına değiyor
veya birkaç piksel yaklaşıyorsa alan/merkez koordinatının kutu tarafından kesilmiş
olabileceğini görünür kılmaktır.

Özellikle doğu Uzunkuyu/Gülbahçe kutusunda yeni bir aday bbox kenarına çok yakın
oluştuğunda yalnız aday merkezinin kutu içinde olması yeterli güvence değildir:
bağlı bileşenin kendisi kenara değebilir. Bu diagnostik, üretimdeki aynı değişim
maskesini yeniden kurar ve bileşen sınırını piksel düzeyinde ölçer. Ada/parsel,
adres veya hukuki statü türetmez.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

import hotspot_coordinate_audit as coordinate_audit
import satellite


REPORT_PATH = Path(__file__).with_name("latest_report.json")
OUTPUT_PATH = Path(__file__).with_name("active_edge_candidate_review.json")
NEAR_EDGE_PIXELS = 2
AREA_SIMILARITY_MIN = coordinate_audit.AREA_SIMILARITY_MIN
MAX_CURRENT_GEOMETRIC_M = coordinate_audit.MAX_CURRENT_GEOMETRIC_M


def _pixel_dimensions_m(bbox, shape):
    height, width = shape
    west, south, east, north = map(float, bbox)
    mean_lat = (south + north) / 2.0
    pixel_width_m = (
        (east - west)
        * 111_320.0
        * math.cos(math.radians(mean_lat))
        / width
    )
    pixel_height_m = (north - south) * 110_570.0 / height
    return float(pixel_height_m), float(pixel_width_m)


def _component_edge_metrics(component, shape, bbox):
    """Bileşenin dört raster kenarına piksel/metre mesafesini döndürür."""
    height, width = shape
    pixels = np.asarray(component, dtype="int32")
    if not len(pixels):
        raise ValueError("Boş bileşen kenar ölçümü yapılamaz.")

    top = int(pixels[:, 0].min())
    bottom = int(height - 1 - pixels[:, 0].max())
    left = int(pixels[:, 1].min())
    right = int(width - 1 - pixels[:, 1].max())
    distances_px = {
        "kuzey": top,
        "guney": bottom,
        "bati": left,
        "dogu": right,
    }
    pixel_height_m, pixel_width_m = _pixel_dimensions_m(bbox, shape)
    distances_m = {
        "kuzey": top * pixel_height_m,
        "guney": bottom * pixel_height_m,
        "bati": left * pixel_width_m,
        "dogu": right * pixel_width_m,
    }
    nearest_side = min(distances_px, key=distances_px.get)
    nearest_px = int(distances_px[nearest_side])
    nearest_m = float(distances_m[nearest_side])
    return {
        "kenar_mesafeleri_piksel": distances_px,
        "en_yakin_kenar": nearest_side,
        "en_yakin_kenar_piksel": nearest_px,
        "en_yakin_kenar_m": round(nearest_m, 1),
        "kenara_temas": nearest_px == 0,
        "kenara_yakin": nearest_px <= NEAR_EDGE_PIXELS,
    }


def _candidate_rows(region_key, report):
    label = satellite.REGIONS[region_key]["label"]
    pair = satellite.sentinel_pair(region_key)
    latest_date = satellite._item_date(pair[1])
    bbox, shape, components, lookup, _score, pixel_area_m2 = (
        coordinate_audit._production_geometry(region_key, pair)
    )

    rows = []
    unmatched = []
    seen = set()
    for raw in report.get("saha_adaylari") or []:
        if not isinstance(raw, dict):
            continue
        # ``yeni_goruntu`` günlük çalışmanın o anda yeni sahne görüp görmediğidir.
        # Aynı son Sentinel sahnesindeki aktif adayın geometrisi ertesi gün de ölçülmelidir;
        # bu yüzden burada bilinçli olarak bu bayrağa göre eleme yapılmaz.
        if raw.get("bolge") != label:
            continue
        if str(raw.get("son_tarih") or "") != latest_date:
            continue
        try:
            area = float(raw.get("alan_m2") or 0)
            latitude = float(raw.get("enlem"))
            longitude = float(raw.get("boylam"))
        except (TypeError, ValueError):
            continue
        if area < satellite.MIN_HOTSPOT_AREA_M2:
            continue

        key = (round(latitude, 6), round(longitude, 6))
        if key in seen:
            continue
        seen.add(key)

        point_pixel = coordinate_audit._point_pixel(latitude, longitude, bbox, shape)
        component_id = lookup.get(point_pixel)
        if component_id is None:
            unmatched.append(
                {
                    "gorev_id": raw.get("gorev_id"),
                    "enlem": round(latitude, 6),
                    "boylam": round(longitude, 6),
                    "alan_m2": round(area),
                    "neden": "rapor_noktasi_guncel_uretim_bilesenine_eslesmedi",
                }
            )
            continue

        component = components[component_id]
        component_area = len(component) * pixel_area_m2
        similarity = coordinate_audit._area_similarity(area, component_area)
        geometric_pixel = coordinate_audit._representative_pixel(component)
        geometric_point = coordinate_audit._pixel_location(
            *geometric_pixel, bbox, shape
        )
        current_point = (round(latitude, 6), round(longitude, 6))
        current_geometric_m = coordinate_audit._distance_m(
            current_point, geometric_point
        )
        if (
            similarity < AREA_SIMILARITY_MIN
            or current_geometric_m > MAX_CURRENT_GEOMETRIC_M
        ):
            unmatched.append(
                {
                    "gorev_id": raw.get("gorev_id"),
                    "enlem": current_point[0],
                    "boylam": current_point[1],
                    "rapor_alan_m2": round(area),
                    "bilesen_alan_m2": round(component_area),
                    "alan_benzerligi": round(similarity, 3),
                    "mevcut_geometrik_fark_m": round(current_geometric_m, 1),
                    "neden": "postprocess_veya_yan_kume_birebir_kenar_olcumu_yapilmadi",
                }
            )
            continue

        edge = _component_edge_metrics(component, shape, bbox)
        rows.append(
            {
                "gorev_id": raw.get("gorev_id"),
                "oncelik": raw.get("oncelik"),
                "mahalle": raw.get("mahalle"),
                "enlem": current_point[0],
                "boylam": current_point[1],
                "alan_m2": round(area),
                "bilesen_alan_m2": round(component_area),
                "bilesen_piksel": len(component),
                "alan_benzerligi": round(similarity, 3),
                **edge,
            }
        )

    rows.sort(
        key=lambda item: (
            int(item.get("en_yakin_kenar_piksel", 10**9)),
            -int(item.get("alan_m2") or 0),
        )
    )
    return {
        "bolge": label,
        "bbox": list(map(float, bbox)),
        "onceki_item": pair[0].get("id"),
        "son_item": pair[1].get("id"),
        "son_tarih": latest_date,
        "olculen_aday": len(rows),
        "kenara_temas_eden": sum(bool(row["kenara_temas"]) for row in rows),
        "kenara_yakin": sum(bool(row["kenara_yakin"]) for row in rows),
        "adaylar": rows,
        "eslesmeyenler": unmatched,
    }


def build_review():
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    regions = {}
    errors = {}
    for region_key in ("cesme", "uzunkuyu"):
        try:
            regions[region_key] = _candidate_rows(region_key, report)
        except Exception as exc:
            errors[region_key] = f"{type(exc).__name__}: {exc}"

    all_rows = [
        row
        for region in regions.values()
        for row in (region.get("adaylar") or [])
    ]
    touching = [row for row in all_rows if row.get("kenara_temas")]
    near = [row for row in all_rows if row.get("kenara_yakin")]
    return {
        "rapor_tarihi": report.get("rapor_tarihi"),
        "amac": (
            "Güncel 250 m²+ ana Sentinel adaylarının bağlı bileşeninin analiz bbox "
            "kenarına değip değmediğini ölçerek alan/merkez koordinatı kırpma riskini görünür kılmak."
        ),
        "politika": (
            "DIAGNOSTIK_ONLY: 250 m² ana eşik, alarm ve saha görevi değişmez. "
            "Kenar teması bbox genişletme/yeniden ölçüm kanıtıdır; tek başına yeni alarm değildir."
        ),
        "ana_uretim_esigi_m2": satellite.MIN_HOTSPOT_AREA_M2,
        "mikro_aralik_m2": [150, 249],
        "kenar_yakin_esigi_piksel": NEAR_EDGE_PIXELS,
        "alarm": False,
        "saha_gorevi": False,
        "olculen_aday": len(all_rows),
        "kenara_temas_eden_aday": len(touching),
        "kenara_yakin_aday": len(near),
        "dikkat_gerekiyor": bool(touching),
        "aksiyon": (
            "BBOX_GENISLETME_VE_YENIDEN_OLCUM_INCELEMESI"
            if touching
            else (
                "SINIR_YAKIN_ADAY_TAKIBI" if near else "EK_AKSIYON_YOK"
            )
        ),
        "bolgeler": regions,
        "hatalar": errors,
    }


def _self_test():
    bbox = [26.45, 38.18, 26.68, 38.43]
    shape = (100, 100)
    touching = [(50, 98), (50, 99), (51, 99)]
    metric = _component_edge_metrics(touching, shape, bbox)
    assert metric["en_yakin_kenar"] == "dogu", metric
    assert metric["en_yakin_kenar_piksel"] == 0, metric
    assert metric["kenara_temas"] is True
    assert metric["kenara_yakin"] is True

    near = [(40, 97), (41, 97)]
    metric = _component_edge_metrics(near, shape, bbox)
    assert metric["en_yakin_kenar_piksel"] == 2, metric
    assert metric["kenara_temas"] is False
    assert metric["kenara_yakin"] is True

    safe = [(45, 50), (46, 51)]
    metric = _component_edge_metrics(safe, shape, bbox)
    assert metric["en_yakin_kenar_piksel"] > NEAR_EDGE_PIXELS, metric
    assert metric["kenara_yakin"] is False
    assert satellite.MIN_HOTSPOT_AREA_M2 == 250
    print("Aktif aday analiz-kenarı koruması öz testi başarılı.")


def _write_if_changed(payload):
    previous = None
    if OUTPUT_PATH.exists():
        try:
            previous = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            previous = None
    if previous == payload:
        print("Aktif aday analiz-kenarı sonucu değişmedi; JSON yeniden yazılmadı.")
        return False
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        return

    _self_test()
    review = build_review()
    _write_if_changed(review)
    print(
        "Aktif aday analiz-kenarı denetimi: "
        f"ölçülen={review['olculen_aday']}, "
        f"temas={review['kenara_temas_eden_aday']}, "
        f"yakın={review['kenara_yakin_aday']}, "
        f"aksiyon={review['aksiyon']}."
    )
    if review.get("hatalar"):
        print("Bölgesel diagnostik hataları:", review["hatalar"])


if __name__ == "__main__":
    main()
