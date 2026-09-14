"""Operasyonel rotadaki koordinatları, adayın kendi Sentinel tarih çiftiyle ölçer.

Bu dosya yalnız diagnostiktir. Alarm, saha görevi, rota sırası, 250 m² ana eşik veya
150–249 m² MİKRO politikasını değiştirmez. Amaç, operasyonel rotada taşınan bir adayın
koordinatını yanlışlıkla başka/güncel olmayan Sentinel çiftiyle doğrulanmış saymamaktır.

Özellikle bölge zarfı ya da sahne seçimi sonradan değiştiğinde ``sentinel_pair`` o anda
başka bir çift döndürebilir. Rota ise daha yeni, tarihsel olarak güvenilir bir adayın
``onceki_tarih``/``son_tarih`` çiftini taşıyor olabilir. Bu audit tam olarak rotadaki bu
tarih çiftini Earth Search kataloğundan yeniden bulur ve aynı üretim maskesinde mevcut
koordinatın bağlı bileşenini ölçer. Sonuç saha kanıtı olmadan koordinatı otomatik taşımaz.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

import hotspot_coordinate_audit as hotspot_audit
import satellite


ROUTE_PATH = Path(__file__).with_name("operational_route.json")
OUTPUT_PATH = Path(__file__).with_name("route_coordinate_precision_audit.json")
MIN_AREA_SIMILARITY = 0.80


def _date_key(value):
    text = str(value or "").strip()
    try:
        day, month, year = (int(part) for part in text.split("."))
        return year, month, day
    except (TypeError, ValueError):
        return 0, 0, 0


def _item_cloud(item):
    try:
        return float(item.get("properties", {}).get("eo:cloud_cover") or 100.0)
    except (TypeError, ValueError):
        return 100.0


def _item_covers_point(item, latitude, longitude):
    bbox = item.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return False
    try:
        west, south, east, north = map(float, bbox[:4])
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return False
    return west <= longitude <= east and south <= latitude <= north


def _route_region_key(raw):
    label = str(raw.get("bolge") or "")
    for region_key in ("cesme", "uzunkuyu"):
        if satellite.REGIONS[region_key]["label"] == label:
            return region_key
    return None


def _pair_rank(older, latest, targets):
    covered = 0
    for raw in targets:
        try:
            latitude = float(raw.get("enlem"))
            longitude = float(raw.get("boylam"))
        except (TypeError, ValueError):
            continue
        if _item_covers_point(older, latitude, longitude) and _item_covers_point(
            latest, latitude, longitude
        ):
            covered += 1

    same_orbit = (
        satellite._relative_orbit(older) is not None
        and satellite._relative_orbit(older) == satellite._relative_orbit(latest)
    )
    # Büyük olan daha iyi: önce hedef kapsaması, sonra aynı yörünge; eşitlikte daha
    # düşük toplam bulut oranını seçmek için negatif değer kullanılır.
    return covered, int(same_orbit), -(_item_cloud(older) + _item_cloud(latest))


def _find_report_pair(region_key, older_date, latest_date, targets, items=None):
    """Rotadaki tarih çiftini katalogdan bul; mevcut bölge zarfına zorla bağlama.

    ``satellite.sentinel_pair`` bugünkü tam-kapsam zarfına göre başka bir çift seçebilir.
    Burada hedef koordinatların iki üründe de gerçekten kaplı olması önceliklidir. Bu,
    eski bir rotayı yanlış sahneyle doğrulama riskini azaltır.
    """
    if not older_date or not latest_date:
        raise satellite.SatelliteError("Rota adayında Sentinel tarih çifti eksik.")

    if items is None:
        bbox = satellite.REGIONS[region_key]["bbox"]
        # Tarihi rotadaki gerçek sahneyi bulmak için global bulut metadata eşiğini
        # diagnostikte gevşetiyoruz. Alarm üretmiyoruz; aşağıda hedefin iki sahnede de
        # fiziksel olarak kapsandığını ayrıca doğruluyoruz.
        items = satellite._search_items(bbox, days=90, max_cloud=100)

    older_items = [item for item in items if satellite._item_date(item) == older_date]
    latest_items = [item for item in items if satellite._item_date(item) == latest_date]
    candidates = []
    for older in older_items:
        for latest in latest_items:
            if not satellite._same_mgrs_tile(older, latest):
                continue
            rank = _pair_rank(older, latest, targets)
            if rank[0] <= 0:
                continue
            candidates.append((rank, older, latest))

    if not candidates:
        raise satellite.SatelliteError(
            f"Rota tarih çifti katalogda hedefi kapsayan aynı-karolu ürünle bulunamadı: "
            f"{older_date} -> {latest_date}"
        )

    candidates.sort(key=lambda row: row[0], reverse=True)
    _rank, older, latest = candidates[0]
    return older, latest


def _analyze_target(raw, region_key, pair, geometry):
    bbox, shape, components, lookup, score, pixel_area_m2 = geometry
    try:
        latitude = float(raw.get("enlem"))
        longitude = float(raw.get("boylam"))
        area = float(raw.get("alan_m2") or 0)
    except (TypeError, ValueError):
        return {
            "gorev_id": raw.get("gorev_id"),
            "durum": "GECERSIZ_KOORDINAT",
        }

    base = {
        "gorev_id": raw.get("gorev_id"),
        "oncelik": raw.get("oncelik"),
        "mahalle": raw.get("mahalle"),
        "bolge": raw.get("bolge"),
        "onceki_tarih": raw.get("onceki_tarih"),
        "son_tarih": raw.get("son_tarih"),
        "alan_m2": round(area),
        "mevcut_enlem": round(latitude, 6),
        "mevcut_boylam": round(longitude, 6),
    }

    if area < satellite.MIN_HOTSPOT_AREA_M2:
        return {**base, "durum": "ANA_ESIK_ALTI_DIAGNOSTIK_DISI"}

    pixel = hotspot_audit._point_pixel(latitude, longitude, bbox, shape)
    component_id = lookup.get(pixel)
    if component_id is None:
        return {
            **base,
            "durum": "HAM_MASK_ESLESMEDI",
            "not": "Mevcut rota koordinatı, adayın kendi tarih çiftinin güncel üretim maskesinde bağlı bileşene düşmedi.",
        }

    component = components[component_id]
    component_area = len(component) * pixel_area_m2
    area_similarity = hotspot_audit._area_similarity(area, component_area)
    geometric_pixel = hotspot_audit._representative_pixel(component)
    weighted_pixel = hotspot_audit._weighted_representative_pixel(component, score)
    geometric_point = hotspot_audit._pixel_location(*geometric_pixel, bbox, shape)
    weighted_point = hotspot_audit._pixel_location(*weighted_pixel, bbox, shape)
    current_point = (round(latitude, 6), round(longitude, 6))
    current_to_geometric = hotspot_audit._distance_m(current_point, geometric_point)
    current_to_weighted = hotspot_audit._distance_m(current_point, weighted_point)

    status = "OLCULDU"
    if area_similarity < MIN_AREA_SIMILARITY:
        status = "POSTPROCESS_RISKI"

    return {
        **base,
        "durum": status,
        "bilesen_alan_m2": round(component_area),
        "alan_benzerligi": round(area_similarity, 3),
        "bilesen_piksel": len(component),
        "geometrik_enlem": geometric_point[0],
        "geometrik_boylam": geometric_point[1],
        "sinyal_agirlikli_enlem": weighted_point[0],
        "sinyal_agirlikli_boylam": weighted_point[1],
        "mevcut_geometrik_fark_m": round(current_to_geometric, 1),
        "mevcut_sinyal_agirlikli_fark_m": round(current_to_weighted, 1),
        "otomatik_tasima": False,
    }


def build_audit():
    route = json.loads(ROUTE_PATH.read_text(encoding="utf-8"))
    targets = [
        raw
        for raw in (route.get("operasyonel_rota") or [])
        if isinstance(raw, dict) and _route_region_key(raw)
    ]

    grouped = defaultdict(list)
    for raw in targets:
        region_key = _route_region_key(raw)
        grouped[(region_key, str(raw.get("onceki_tarih") or ""), str(raw.get("son_tarih") or ""))].append(raw)

    results = []
    pairs = []
    errors = []
    for (region_key, older_date, latest_date), rows in sorted(
        grouped.items(), key=lambda item: (_date_key(item[0][2]), item[0][0]), reverse=True
    ):
        try:
            pair = _find_report_pair(region_key, older_date, latest_date, rows)
            geometry = hotspot_audit._production_geometry(region_key, pair)
            pairs.append(
                {
                    "bolge": satellite.REGIONS[region_key]["label"],
                    "onceki_tarih": older_date,
                    "son_tarih": latest_date,
                    "onceki_item": pair[0].get("id"),
                    "son_item": pair[1].get("id"),
                    "hedef_sayisi": len(rows),
                }
            )
            for raw in rows:
                results.append(_analyze_target(raw, region_key, pair, geometry))
        except Exception as exc:
            errors.append(
                {
                    "bolge": satellite.REGIONS[region_key]["label"],
                    "onceki_tarih": older_date,
                    "son_tarih": latest_date,
                    "hedef_sayisi": len(rows),
                    "hata": f"{type(exc).__name__}: {exc}",
                }
            )

    measured = [row for row in results if row.get("durum") == "OLCULDU"]
    shifts = [float(row.get("mevcut_sinyal_agirlikli_fark_m") or 0) for row in measured]
    return {
        "rapor_tarihi": route.get("rapor_tarihi"),
        "kaynak_rapor_olusturma": route.get("kaynak_rapor_olusturma"),
        "politika": "DIAGNOSTIK_ONLY: koordinat otomatik taşınmaz; alarm/görev/sıra/250 m² ana eşik/150–249 m² MİKRO politikası değişmez.",
        "rota_hedefi": len(targets),
        "olculen": len(measured),
        "postprocess_riski": sum(1 for row in results if row.get("durum") == "POSTPROCESS_RISKI"),
        "ham_mask_eslesmedi": sum(1 for row in results if row.get("durum") == "HAM_MASK_ESLESMEDI"),
        "10m_ustu_mevcut_sinyal_farki": sum(1 for value in shifts if value > 10),
        "20m_ustu_mevcut_sinyal_farki": sum(1 for value in shifts if value > 20),
        "maksimum_mevcut_sinyal_farki_m": round(max(shifts or [0.0]), 1),
        "sahne_ciftleri": pairs,
        "hedefler": results,
        "hatalar": errors,
    }


def _self_test():
    target = {
        "bolge": satellite.REGIONS["cesme"]["label"],
        "enlem": 38.28,
        "boylam": 26.37,
    }

    def item(item_id, date_text, bbox, orbit, cloud):
        day, month, year = date_text.split(".")
        return {
            "id": item_id,
            "bbox": bbox,
            "properties": {
                "datetime": f"{year}-{month}-{day}T09:00:00Z",
                "sat:relative_orbit": orbit,
                "eo:cloud_cover": cloud,
                "s2:mgrs_tile": "35SMC",
            },
            "assets": {name: {} for name in ("visual", "red", "nir", "scl")},
        }

    full = [26.2, 38.1, 26.5, 38.5]
    far = [26.6, 38.1, 26.8, 38.5]
    items = [
        item("old-good", "08.09.2026", full, 22, 40),
        item("new-good", "13.09.2026", full, 22, 55),
        item("old-far", "08.09.2026", far, 22, 1),
        item("new-far", "13.09.2026", far, 22, 1),
    ]
    pair = _find_report_pair(
        "cesme", "08.09.2026", "13.09.2026", [target], items=items
    )
    assert pair[0]["id"] == "old-good"
    assert pair[1]["id"] == "new-good"
    assert _date_key("13.09.2026") > _date_key("08.09.2026")
    assert _route_region_key(target) == "cesme"
    print("Operasyonel rota koordinat piksel denetimi öz testi başarılı.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        return

    audit = build_audit()
    OUTPUT_PATH.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "Rota koordinat audit: "
        f"hedef {audit['rota_hedefi']}, ölçülen {audit['olculen']}, "
        f"ham-mask eşleşmedi {audit['ham_mask_eslesmedi']}, "
        f">10 m {audit['10m_ustu_mevcut_sinyal_farki']}, "
        f">20 m {audit['20m_ustu_mevcut_sinyal_farki']}."
    )
    if audit.get("hatalar"):
        print("Diagnostik hatalar:", audit["hatalar"])


if __name__ == "__main__":
    main()
