"""Sentinel aday tavanının ve 8-komşu geometrinin erken şantiye sinyallerini gizleyip gizlemediğini ölçer.

Bu denetim alarm üretmez, saha görevi açmaz ve uydu eşiklerini değiştirmez. Ana motorun
aynı 250 m² / 10 m / spektral kurallarıyla hem normal 24 adaylık çıktıyı hem de yalnız
aday sayısı tavanı kaldırılmış çıktıyı aynı görüntü üzerinde yeniden hesaplar. Ayrıca
nihai değişim maskesinde yalnız köşeden temas eden piksellerin 8-komşuluk nedeniyle tek
bir büyük kümeye birleşip birleşmediğini 4-komşu karşılaştırmasıyla ölçer. Böylece
parsel ölçeğindeki ayrı kazıların tek geniş aday içinde gizlenmesi üretim algoritmasına
dokunmadan sayısal olarak izlenebilir.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np

import satellite
from daily_report import ISTANBUL, REPORT_REGIONS, ensure_daily_schema
from scanner import connect


AUDIT_FILE = Path(__file__).with_name("candidate_capacity_audit.json")
RAW_LIMIT = 1_000_000
CONSTRUCTION_SCALE_MIN_M2 = satellite.SMALL_HOTSPOT_MAX_M2
CONSTRUCTION_SCALE_MAX_M2 = 10_000
CONNECTIVITY_EXAMPLE_LIMIT = 5

# analyze_sentinel_change çalışma anında satellite._hotspots adını çözer. Bu
# referansı saklayarak yalnız bu denetim sürecinde aday tavanını kaldırabiliyoruz;
# üretim algoritmasına veya DB'deki adaylara dokunulmuyor.
_ORIGINAL_HOTSPOTS = satellite._hotspots


def _uncapped_hotspots(
    change_mask,
    bbox,
    pixel_area_m2,
    small_site_mask=None,
    limit=satellite.HOTSPOT_LIMIT,
    small_quota=satellite.SMALL_HOTSPOT_QUOTA,
):
    del limit, small_quota
    return _ORIGINAL_HOTSPOTS(
        change_mask,
        bbox,
        pixel_area_m2,
        small_site_mask=small_site_mask,
        limit=RAW_LIMIT,
        small_quota=0,
    )


def _scale_bucket(item):
    try:
        area = float(item.get("alan_m2") or 0)
    except (TypeError, ValueError):
        area = 0.0
    if area < CONSTRUCTION_SCALE_MIN_M2:
        return "kucuk_250_800"
    if area <= CONSTRUCTION_SCALE_MAX_M2:
        return "santiye_olcegi_800_10000"
    return "genis_10000_ustu"


def _bucket_counts(items):
    counts = {
        "kucuk_250_800": 0,
        "santiye_olcegi_800_10000": 0,
        "genis_10000_ustu": 0,
    }
    for item in items:
        if isinstance(item, dict):
            counts[_scale_bucket(item)] += 1
    return counts


def _candidate_key(item):
    try:
        return (
            round(float(item.get("enlem")), 6),
            round(float(item.get("boylam")), 6),
            round(float(item.get("alan_m2") or 0)),
        )
    except (TypeError, ValueError, AttributeError):
        return None


def _difference(left, right):
    """left içinde olup right içinde olmayan adayları deterministik koordinat+alanla bul."""
    right_keys = {key for key in map(_candidate_key, right) if key is not None}
    return [
        item
        for item in left
        if (key := _candidate_key(item)) is not None and key not in right_keys
    ]


def _four_connected_components(mask):
    """Yalnız kenardan temas eden pikselleri aynı kümede tutan 4-komşu bileşenler."""
    rows, columns = np.nonzero(mask)
    if not len(rows):
        return []

    height, width = mask.shape
    visited = np.zeros(mask.shape, dtype=bool)
    neighbors = ((-1, 0), (0, -1), (0, 1), (1, 0))
    components = []

    for seed_row, seed_col in zip(rows.tolist(), columns.tolist()):
        if visited[seed_row, seed_col]:
            continue
        visited[seed_row, seed_col] = True
        stack = [(seed_row, seed_col)]
        component = []
        while stack:
            row, column = stack.pop()
            component.append((row, column))
            for row_offset, col_offset in neighbors:
                next_row = row + row_offset
                next_col = column + col_offset
                if not (0 <= next_row < height and 0 <= next_col < width):
                    continue
                if not mask[next_row, next_col] or visited[next_row, next_col]:
                    continue
                visited[next_row, next_col] = True
                stack.append((next_row, next_col))
        components.append(component)
    return components


def _eligible_component(component, pixel_area_m2, small_site_mask=None):
    area_m2 = len(component) * pixel_area_m2
    if area_m2 < satellite.MIN_HOTSPOT_AREA_M2:
        return False
    if area_m2 >= satellite.SMALL_HOTSPOT_MAX_M2:
        return True
    if small_site_mask is None:
        return False
    pixels = np.asarray(component, dtype="int32")
    strong_fraction = float(
        np.mean(small_site_mask[pixels[:, 0], pixels[:, 1]])
    )
    return strong_fraction >= 0.50


def _component_point(component, bbox, shape):
    pixels = np.asarray(component, dtype="int32")
    centroid = pixels.mean(axis=0)
    representative = pixels[int(np.argmin(np.sum((pixels - centroid) ** 2, axis=1)))]
    row, column = int(representative[0]), int(representative[1])
    height, width = shape
    west, south, east, north = bbox
    latitude = north - (row + 0.5) / height * (north - south)
    longitude = west + (column + 0.5) / width * (east - west)
    return round(latitude, 6), round(longitude, 6)


def _connectivity_metrics(change_mask, bbox, pixel_area_m2, small_site_mask=None):
    """8-komşu bir adayın yalnız diyagonal temas yüzünden ayrı 4-komşu adayları gizlemesini ölç."""
    eight_components = satellite._connected_components(change_mask)
    four_components = _four_connected_components(change_mask)

    parent_by_pixel = {}
    eligible_eight = set()
    parent_area = {}
    for parent_index, component in enumerate(eight_components):
        for pixel in component:
            parent_by_pixel[pixel] = parent_index
        area_m2 = len(component) * pixel_area_m2
        parent_area[parent_index] = area_m2
        if _eligible_component(component, pixel_area_m2, small_site_mask):
            eligible_eight.add(parent_index)

    children_by_parent = {}
    eligible_four = []
    for component in four_components:
        if not _eligible_component(component, pixel_area_m2, small_site_mask):
            continue
        parent_index = parent_by_pixel.get(component[0])
        if parent_index is None:
            continue
        children_by_parent.setdefault(parent_index, []).append(component)
        eligible_four.append(component)

    split_parents = []
    recovered_small = 0
    recovered_construction = 0
    recovered_from_wide = 0
    examples = []
    for parent_index, children in children_by_parent.items():
        if parent_index not in eligible_eight or len(children) < 2:
            continue
        split_parents.append(parent_index)
        child_areas = sorted(
            (round(len(component) * pixel_area_m2) for component in children),
        )
        for child_area in child_areas:
            if child_area < satellite.SMALL_HOTSPOT_MAX_M2:
                recovered_small += 1
            elif child_area <= CONSTRUCTION_SCALE_MAX_M2:
                recovered_construction += 1
            if (
                parent_area[parent_index] > CONSTRUCTION_SCALE_MAX_M2
                and satellite.MIN_HOTSPOT_AREA_M2 <= child_area <= CONSTRUCTION_SCALE_MAX_M2
            ):
                recovered_from_wide += 1

        if len(examples) < CONNECTIVITY_EXAMPLE_LIMIT:
            latitude, longitude = _component_point(
                eight_components[parent_index], bbox, change_mask.shape
            )
            examples.append(
                {
                    "enlem": latitude,
                    "boylam": longitude,
                    "sekiz_komsu_alan_m2": round(parent_area[parent_index]),
                    "dort_komsu_alt_aday_sayisi": len(children),
                    "dort_komsu_alt_alanlar_m2": child_areas[:12],
                }
            )

    return {
        "sekiz_komsu_gecerli_aday": len(eligible_eight),
        "dort_komsu_gecerli_aday": len(eligible_four),
        "diyagonal_birlesmis_ebeveyn": len(split_parents),
        "ayrisan_kucuk_250_800": recovered_small,
        "ayrisan_santiye_olcegi_800_10000": recovered_construction,
        "genis_ebeveynden_ayrisan_250_10000": recovered_from_wide,
        "ornekler": examples,
        "yorum": (
            "4-komşu değerleri üretim alarmı değildir; yalnız köşeden temasın 8-komşu "
            "kümeleri birleştirip birleştirmediğini ölçer."
        ),
    }


def _self_check():
    # 30 ayrı güçlü ~300 m² küme üret. Üretim yolu 24'te kesilmeli; denetim yolu
    # aynı eşiklerle 30'un tamamını görmeli. Böylece denetimin yanlışlıkla yeni bir
    # algılama eşiği tanımlamadığı ve yalnız kapasite tavanını ölçtüğü doğrulanır.
    signal = np.zeros((61, 8), dtype=bool)
    for index in range(30):
        row = 1 + index * 2
        signal[row, 2:5] = True
    bbox = [26.30, 38.20, 26.31, 38.26]
    capped = _ORIGINAL_HOTSPOTS(
        signal,
        bbox,
        100.0,
        small_site_mask=signal,
    )
    uncapped = _uncapped_hotspots(
        signal,
        bbox,
        100.0,
        small_site_mask=signal,
    )
    assert len(capped) == satellite.HOTSPOT_LIMIT, (
        "Sentinel üretim aday tavanı beklenen 24'lük sınırda değil."
    )
    assert len(uncapped) == 30, (
        "Kapasite denetimi üretim eşiklerini değiştirmeden tavan dışı adayları göremiyor."
    )
    assert len(_difference(uncapped, capped)) == 6, (
        "Kapasite kaybı ile çıktı sonrası eleme ayrımı bozuldu."
    )
    assert _scale_bucket({"alan_m2": 500}) == "kucuk_250_800"
    assert _scale_bucket({"alan_m2": 5000}) == "santiye_olcegi_800_10000"
    assert _scale_bucket({"alan_m2": 20000}) == "genis_10000_ustu"

    # İki 2x2 blok yalnız köşeden temas ediyor. 8-komşuluk bunu tek 800 m² aday
    # yaparken 4-komşuluk iki ayrı 400 m² güçlü küçük-saha adayı olarak ayırmalı.
    diagonal = np.zeros((7, 7), dtype=bool)
    diagonal[1:3, 1:3] = True
    diagonal[3:5, 3:5] = True
    geometry = _connectivity_metrics(
        diagonal,
        [26.30, 38.20, 26.31, 38.21],
        100.0,
        small_site_mask=diagonal,
    )
    assert geometry["sekiz_komsu_gecerli_aday"] == 1, geometry
    assert geometry["dort_komsu_gecerli_aday"] == 2, geometry
    assert geometry["diyagonal_birlesmis_ebeveyn"] == 1, geometry
    assert geometry["ayrisan_kucuk_250_800"] == 2, geometry


def _stored_snapshot(report_date):
    snapshots = {}
    with connect() as connection:
        for region_key in REPORT_REGIONS:
            row = connection.execute(
                """SELECT son_item,hareket_json,hata FROM gunluk_uydu_raporlari
                WHERE rapor_tarihi=? AND bolge=? LIMIT 1""",
                (report_date, region_key),
            ).fetchone()
            if not row:
                snapshots[region_key] = {
                    "son_item": None,
                    "hareket": [],
                    "hata": "rapor_yok",
                }
                continue
            try:
                movement = json.loads(row[1] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                movement = []
            if not isinstance(movement, list):
                movement = []
            snapshots[region_key] = {
                "son_item": row[0],
                "hareket": [item for item in movement if isinstance(item, dict)],
                "hata": row[2],
            }
    return snapshots


def _analyze_with_hotspot_function(region_key, pair, function):
    original = satellite._hotspots
    satellite._hotspots = function
    try:
        return satellite.analyze_sentinel_change(region_key, pair=pair)
    finally:
        satellite._hotspots = original


def audit_capacity():
    ensure_daily_schema()
    _self_check()
    now = datetime.now(ISTANBUL)
    report_date = now.strftime("%Y-%m-%d")
    stored = _stored_snapshot(report_date)
    regions = {}

    for region_key in REPORT_REGIONS:
        snapshot = stored.get(region_key, {})
        record = {
            "bolge": satellite.REGIONS[region_key]["label"],
            "son_item": snapshot.get("son_item"),
            "durum": "ok",
        }
        if snapshot.get("hata"):
            record["durum"] = "gunluk_uydu_hatasi"
            record["hata"] = str(snapshot.get("hata"))
            regions[region_key] = record
            continue
        try:
            pair = satellite.sentinel_pair(region_key)
            older, latest = pair
            record["latest_item"] = latest.get("id")
            if snapshot.get("son_item") != latest.get("id"):
                record["durum"] = "gunluk_rapor_latest_ile_eslesmiyor"
                regions[region_key] = record
                continue

            # Aynı sahneyi iki kez ölç: önce gerçek üretim 24 tavanıyla, sonra
            # yalnız tavan kaldırılarak. İkinci geçişte nihai maskeyi de yakalayıp
            # 8-komşu / 4-komşu geometrisini ek raster okuması yapmadan ölçüyoruz.
            capped_result = _analyze_with_hotspot_function(
                region_key,
                pair,
                _ORIGINAL_HOTSPOTS,
            )
            connectivity = {}

            def uncapped_with_geometry(
                change_mask,
                bbox,
                pixel_area_m2,
                small_site_mask=None,
                limit=satellite.HOTSPOT_LIMIT,
                small_quota=satellite.SMALL_HOTSPOT_QUOTA,
            ):
                del limit, small_quota
                connectivity.update(
                    _connectivity_metrics(
                        change_mask,
                        bbox,
                        pixel_area_m2,
                        small_site_mask=small_site_mask,
                    )
                )
                return _uncapped_hotspots(
                    change_mask,
                    bbox,
                    pixel_area_m2,
                    small_site_mask=small_site_mask,
                )

            raw_result = _analyze_with_hotspot_function(
                region_key,
                pair,
                uncapped_with_geometry,
            )
            capped = [
                item for item in capped_result.get("hotspots", [])
                if isinstance(item, dict)
            ]
            raw = [
                item for item in raw_result.get("hotspots", [])
                if isinstance(item, dict)
            ]
            report_kept = list(snapshot.get("hareket") or [])
            dropped_by_cap = _difference(raw, capped)
            changed_by_selection = _difference(capped, report_kept)

            record.update(
                {
                    "aday_tavani": satellite.HOTSPOT_LIMIT,
                    "ham_uygun_aday": len(raw),
                    "tavan_sonrasi_aday": len(capped),
                    "raporda_kalan_aday": len(report_kept),
                    "tavan_disinda_kalan": len(dropped_by_cap),
                    "ham_secimden_farkli": len(changed_by_selection),
                    "tavana_ulasti": len(raw) > satellite.HOTSPOT_LIMIT,
                    "ham_olcek_dagilimi": _bucket_counts(raw),
                    "tavan_sonrasi_olcek_dagilimi": _bucket_counts(capped),
                    "raporda_olcek_dagilimi": _bucket_counts(report_kept),
                    "tavan_disinda_olcek_dagilimi": _bucket_counts(dropped_by_cap),
                    "ham_secimden_farkli_olcek_dagilimi": _bucket_counts(
                        changed_by_selection
                    ),
                    "baglanti_geometrisi": connectivity,
                }
            )
        except Exception as exc:
            record["durum"] = "denetim_hatasi"
            record["hata"] = f"{type(exc).__name__}: {exc}"
        regions[region_key] = record

    payload = {
        "rapor_tarihi": report_date,
        "olusturma": now.strftime("%Y-%m-%d %H:%M %Z"),
        "amac": (
            "24 aday tavanının ve 8-komşu piksel geometrisinin 250 m²+ geçerli "
            "Sentinel kümelerini gizleyip gizlemediğini ölçmek; bu dosya alarm veya "
            "saha görevi üretmez."
        ),
        "diagnostik_santiye_olcegi_m2": [
            CONSTRUCTION_SCALE_MIN_M2,
            CONSTRUCTION_SCALE_MAX_M2,
        ],
        "bolgeler": regions,
    }
    AUDIT_FILE.write_text(
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
        print(
            "Aday kapasite denetimi kalite kontrolü başarılı: üretim 24 tavanı korunuyor; "
            "denetim aynı 250 m²+ filtrelerle pre-dedupe tavan kaybını ve diyagonal "
            "8-komşu birleşme riskini ayrı ölçüyor."
        )
        return

    payload = audit_capacity()
    summaries = []
    for key, item in payload["bolgeler"].items():
        if item.get("durum") != "ok":
            summaries.append(f"{key}: {item.get('durum')}")
            continue
        dropped_scale = item.get("tavan_disinda_olcek_dagilimi", {})
        geometry = item.get("baglanti_geometrisi", {})
        summaries.append(
            f"{key}: ham {item.get('ham_uygun_aday', 0)} → tavan "
            f"{item.get('tavan_sonrasi_aday', 0)} → rapor {item.get('raporda_kalan_aday', 0)}; "
            f"tavan dışı {item.get('tavan_disinda_kalan', 0)} "
            f"(250-800={dropped_scale.get('kucuk_250_800', 0)}, "
            f"800-10000={dropped_scale.get('santiye_olcegi_800_10000', 0)}, "
            f">10000={dropped_scale.get('genis_10000_ustu', 0)}); "
            f"diyagonal birleşmiş ebeveyn={geometry.get('diyagonal_birlesmis_ebeveyn', 0)}, "
            f"genişten ayrışabilecek 250-10000={geometry.get('genis_ebeveynden_ayrisan_250_10000', 0)}"
        )
    print("Aday kapasite/geometri denetimi: " + " | ".join(summaries))


if __name__ == "__main__":
    main()
