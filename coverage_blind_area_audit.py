"""Sentinel ana + zaman-serisi katmanlarından sonra kalan gerçek mekânsal kör alanı ölçer.

Bu denetim alarm veya saha görevi üretmez. Ana Sentinel karşılaştırmasının göremediği
piksellerden mevcut temporal_gap_scan ve latest_cloud_gap_scan mantığıyla geri
kazanılabilenleri düşer. Ayrıca üretim mantığını değiştirmeden, tek yedek yerine
birkaç uygun açık tarih kullanılsaydı yalnız piksel kapsaması bakımından ne kadar
ek kör alanın geri kazanılabileceğini ölçer. Bu ek ölçüm aday üretmez ve spektral
değişim kanıtı sayılmaz.

Deniz/bulut sınırını kara sanmamak için kara hedefi, aynı Sentinel karosundaki son
açık tarihlerin SCL=4 (vegetation) veya SCL=5 (non-vegetated) kanıtından bağımsız
olarak kurulur. Böylece yalnız gerçekten daha önce kara olarak gözlenmiş 10 m
piksellerde kalan 250 m²+ kör kümeler raporlanır; hiçbir referans sahnede kara/su
olarak çözülemeyen yüzey ayrıca ölçülür.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

from daily_report import ISTANBUL, REPORT_REGIONS
from satellite import (
    MIN_HOTSPOT_AREA_M2,
    REGIONS,
    _connected_components,
    _item_covers_bbox,
    _nearest_place,
    _output_shape,
    _read_asset,
    _relative_orbit,
    _same_mgrs_tile,
    _search_items,
    sentinel_pair,
)
from temporal_gap_scan import (
    EXCLUDED_CLASSES,
    FALLBACK_MIN_GAP_DAYS,
    TRANSIENT_OLDER_CLASSES,
    select_fallback,
)


OUTPUT_FILE = Path(__file__).with_name("coverage_blind_area_audit.json")
MAX_EXAMPLES = 12
MIN_COMPONENT_PIXELS = 3
LAND_REFERENCE_SCENES = 8
MULTI_FALLBACK_SCENES = 4
TRANSIENT_CLASSES = TRANSIENT_OLDER_CLASSES
LAND_CLASSES = np.array([4, 5], dtype="uint8")
WATER_CLASS = 6
NO_DATA_CLASS = 0


def _pixel_area_m2(bbox, height, width):
    west, south, east, north = bbox
    pixel_width_m = (
        (east - west)
        * 111320
        * math.cos(math.radians((south + north) / 2))
        / width
    )
    pixel_height_m = (north - south) * 110570 / height
    return pixel_width_m * pixel_height_m


def _item_time(item):
    return datetime.fromisoformat(
        item["properties"]["datetime"].replace("Z", "+00:00")
    )


def _item_date(item):
    return _item_time(item).strftime("%d.%m.%Y")


def _reference_items(items, latest, bbox, limit=LAND_REFERENCE_SCENES):
    refs = []
    seen = set()
    for item in items:
        item_id = str(item.get("id") or "")
        if not item_id or item_id in seen:
            continue
        if not _same_mgrs_tile(item, latest):
            continue
        if not _item_covers_bbox(item, bbox):
            continue
        refs.append(item)
        seen.add(item_id)
        if len(refs) >= limit:
            break
    return refs


def _fallback_items(items, latest, primary, bbox, preferred, limit=MULTI_FALLBACK_SCENES):
    """Üretimdeki yedek seçimini bozmadan birkaç güvenli yedeği audit için sırala.

    Aynı karo, tam bbox kapsaması ve mevcut 7+ gün kuralı korunur. İlk sıraya
    select_fallback'ın seçtiği üretim yedeği konur; kalanlarda aynı göreli yörünge
    tercih edilir. Bu liste yalnız kapsama ölçümü için kullanılır.
    """
    latest_time = _item_time(latest)
    latest_orbit = _relative_orbit(latest)
    candidates = []
    seen = set()
    for item in items:
        item_id = str(item.get("id") or "")
        if not item_id or item_id in seen:
            continue
        if item_id in {latest.get("id"), primary.get("id")}:
            continue
        if not _same_mgrs_tile(item, latest):
            continue
        if not _item_covers_bbox(item, bbox):
            continue
        age_days = (latest_time - _item_time(item)).total_seconds() / 86400
        if age_days < FALLBACK_MIN_GAP_DAYS:
            continue
        candidates.append(item)
        seen.add(item_id)

    candidates.sort(key=_item_time, reverse=True)
    same_orbit = [item for item in candidates if _relative_orbit(item) == latest_orbit]
    other_orbit = [item for item in candidates if item not in same_orbit]
    ordered = same_orbit + other_orbit if latest_orbit is not None else candidates

    if preferred is not None:
        preferred_id = preferred.get("id")
        ordered = [preferred] + [item for item in ordered if item.get("id") != preferred_id]

    return ordered[:limit]


def _historical_surface_mask(items, latest, bbox, height, width):
    """Yakın tarihlerden bağımsız kara/su kanıtı üretir.

    SCL=7 unclassified bilinçli olarak kara kanıtı sayılmaz. En az bir açık SCL=4/5
    gözlemi kara için yeterlidir; hiç kara kanıtı yokken en az bir SCL=6 gözlemi su
    sayılır. Tüm referanslarda yalnız bulut/gölge/no-data vb. kalan piksel
    `unknown_surface` olarak tutulur ve kara körlüğüne karıştırılmaz.
    """
    refs = _reference_items(items, latest, bbox)
    land_seen = np.zeros((height, width), dtype=bool)
    water_seen = np.zeros((height, width), dtype=bool)
    dates = []
    for item in refs:
        scl = _read_asset(item, "scl", bbox, height, width, "nearest")[0]
        land_seen |= np.isin(scl, LAND_CLASSES)
        water_seen |= scl == WATER_CLASS
        dates.append(_item_date(item))

    known_land = land_seen
    known_water = ~land_seen & water_seen
    unknown_surface = ~land_seen & ~water_seen
    return known_land, known_water, unknown_surface, refs, dates


def _component_example(component, mask, bbox, pixel_area_m2, primary_scl, latest_scl, fallback_scl):
    pixels = np.asarray(component, dtype="int32")
    centroid = pixels.mean(axis=0)
    representative = pixels[int(np.argmin(np.sum((pixels - centroid) ** 2, axis=1)))]
    row, column = int(representative[0]), int(representative[1])
    height, width = mask.shape
    west, south, east, north = bbox
    latitude = north - (row + 0.5) / height * (north - south)
    longitude = west + (column + 0.5) / width * (east - west)

    transient = np.isin(primary_scl[pixels[:, 0], pixels[:, 1]], TRANSIENT_CLASSES)
    transient |= np.isin(latest_scl[pixels[:, 0], pixels[:, 1]], TRANSIENT_CLASSES)
    no_data = primary_scl[pixels[:, 0], pixels[:, 1]] == NO_DATA_CLASS
    no_data |= latest_scl[pixels[:, 0], pixels[:, 1]] == NO_DATA_CLASS
    if fallback_scl is not None:
        transient |= np.isin(fallback_scl[pixels[:, 0], pixels[:, 1]], TRANSIENT_CLASSES)
        no_data |= fallback_scl[pixels[:, 0], pixels[:, 1]] == NO_DATA_CLASS

    if float(np.mean(no_data)) >= 0.50:
        reason = "NO_DATA_KENAR_RISKI"
    elif float(np.mean(transient)) >= 0.50:
        reason = "BULUT_GOLGE_KALICI"
    else:
        reason = "KARISIK_GECERSIZLIK"

    return {
        "mahalle_yaklasik": _nearest_place(latitude, longitude),
        "enlem": round(latitude, 6),
        "boylam": round(longitude, 6),
        "alan_m2": round(len(component) * pixel_area_m2),
        "neden": reason,
    }


def _component_summary(mask, bbox, pixel_area_m2, primary_scl, latest_scl, fallback_scl):
    components = []
    for component in _connected_components(mask):
        if len(component) < MIN_COMPONENT_PIXELS:
            continue
        area_m2 = len(component) * pixel_area_m2
        if area_m2 < MIN_HOTSPOT_AREA_M2:
            continue
        components.append(
            _component_example(
                component,
                mask,
                bbox,
                pixel_area_m2,
                primary_scl,
                latest_scl,
                fallback_scl,
            )
        )
    components.sort(key=lambda item: item["alan_m2"], reverse=True)
    return components


def _analyze_region(region_key):
    bbox = REGIONS[region_key]["bbox"]
    primary, latest = sentinel_pair(region_key)
    height, width = _output_shape(bbox)
    pixel_area_m2 = _pixel_area_m2(bbox, height, width)

    primary_scl = _read_asset(primary, "scl", bbox, height, width, "nearest")[0]
    latest_scl = _read_asset(latest, "scl", bbox, height, width, "nearest")[0]

    items = _search_items(bbox)
    fallback = select_fallback(items, latest, primary, bbox=bbox)
    fallback_items = _fallback_items(items, latest, primary, bbox, fallback)
    fallback_scl = None
    fallback_scls = []
    for index, item in enumerate(fallback_items):
        scl = _read_asset(item, "scl", bbox, height, width, "nearest")[0]
        fallback_scls.append(scl)
        if index == 0:
            fallback_scl = scl

    known_land, known_water, unknown_surface, refs, ref_dates = _historical_surface_mask(
        items, latest, bbox, height, width
    )
    if not refs:
        raise RuntimeError("Tarihsel kara/su referansı için tam-kapsam Sentinel sahnesi yok.")

    primary_valid = ~np.isin(primary_scl, EXCLUDED_CLASSES)
    latest_valid = ~np.isin(latest_scl, EXCLUDED_CLASSES)
    main_valid = primary_valid & latest_valid

    pair_blind = known_land & ~main_valid
    recovered = main_valid.copy()
    multi_recovered = main_valid.copy()

    primary_transient = np.isin(primary_scl, TRANSIENT_CLASSES)
    latest_transient = np.isin(latest_scl, TRANSIENT_CLASSES)

    if fallback_scl is not None:
        fallback_valid = ~np.isin(fallback_scl, EXCLUDED_CLASSES)
        recovered |= primary_transient & latest_valid & fallback_valid
        recovered |= latest_transient & primary_valid & fallback_valid

    for scl in fallback_scls:
        fallback_valid = ~np.isin(scl, EXCLUDED_CLASSES)
        multi_recovered |= primary_transient & latest_valid & fallback_valid
        multi_recovered |= latest_transient & primary_valid & fallback_valid

    residual = known_land & ~recovered
    multi_residual = known_land & ~multi_recovered
    recovered_from_pair_blind = pair_blind & recovered
    multi_recovered_from_pair_blind = pair_blind & multi_recovered
    extra_multi_recovered = residual & multi_recovered

    components = _component_summary(
        residual, bbox, pixel_area_m2, primary_scl, latest_scl, fallback_scl
    )
    multi_components = _component_summary(
        multi_residual, bbox, pixel_area_m2, primary_scl, latest_scl, fallback_scl
    )
    unknown_components = _component_summary(
        unknown_surface, bbox, pixel_area_m2, primary_scl, latest_scl, fallback_scl
    )

    known_land_pixels = max(int(known_land.sum()), 1)
    total_pixels = max(int(known_land.size), 1)
    pair_blind_pixels = int(pair_blind.sum())
    recovered_pixels = int(recovered_from_pair_blind.sum())
    residual_pixels = int(residual.sum())
    multi_recovered_pixels = int(multi_recovered_from_pair_blind.sum())
    extra_multi_recovered_pixels = int(extra_multi_recovered.sum())
    multi_residual_pixels = int(multi_residual.sum())
    unknown_pixels = int(unknown_surface.sum())

    return {
        "bolge": REGIONS[region_key]["label"],
        "ana_onceki_item": primary["id"],
        "ana_onceki_tarih": _item_date(primary),
        "son_item": latest["id"],
        "son_tarih": _item_date(latest),
        "yedek_item": fallback["id"] if fallback is not None else None,
        "yedek_tarih": _item_date(fallback) if fallback is not None else None,
        "coklu_yedek_sahne_sayisi": len(fallback_items),
        "coklu_yedek_tarihleri": [_item_date(item) for item in fallback_items],
        "analiz_piksel_m_yaklasik": round(math.sqrt(pixel_area_m2), 2),
        "kara_referans_sahne_sayisi": len(refs),
        "kara_referans_tarihleri": ref_dates,
        "kara_hedef_piksel": known_land_pixels,
        "su_referansli_piksel": int(known_water.sum()),
        "cozumlenmemis_yuzey_piksel": unknown_pixels,
        "cozumlenmemis_yuzey_yuzde": round(unknown_pixels / total_pixels * 100, 4),
        "cozumlenmemis_250m2_ustu_kume": len(unknown_components),
        "ana_cift_kor_piksel": pair_blind_pixels,
        "ana_cift_kor_yuzde": round(pair_blind_pixels / known_land_pixels * 100, 4),
        "zaman_serisiyle_geri_kazanilan_piksel": recovered_pixels,
        "zaman_serisiyle_geri_kazanilan_yuzde": round(recovered_pixels / known_land_pixels * 100, 4),
        "kalan_kor_piksel": residual_pixels,
        "kalan_kor_yuzde": round(residual_pixels / known_land_pixels * 100, 4),
        "kalan_250m2_ustu_kume": len(components),
        "en_buyuk_kalan_kor_alan_m2": max((item["alan_m2"] for item in components), default=0),
        "coklu_yedek_toplam_geri_kazanilan_piksel": multi_recovered_pixels,
        "coklu_yedek_ek_geri_kazanilan_piksel": extra_multi_recovered_pixels,
        "coklu_yedek_ek_geri_kazanilan_yuzde": round(extra_multi_recovered_pixels / known_land_pixels * 100, 4),
        "coklu_yedek_sonrasi_kalan_kor_piksel": multi_residual_pixels,
        "coklu_yedek_sonrasi_kalan_kor_yuzde": round(multi_residual_pixels / known_land_pixels * 100, 4),
        "coklu_yedek_sonrasi_250m2_ustu_kume": len(multi_components),
        "coklu_yedek_sonrasi_en_buyuk_kor_alan_m2": max(
            (item["alan_m2"] for item in multi_components), default=0
        ),
        "ornekler": components[:MAX_EXAMPLES],
        "coklu_yedek_sonrasi_ornekler": multi_components[:MAX_EXAMPLES],
        "cozumlenmemis_yuzey_ornekleri": unknown_components[:MAX_EXAMPLES],
        "durum": "ok" if fallback is not None else "yedek_sahne_yok",
    }


def _core_payload():
    regions = {}
    errors = []
    for region_key in REPORT_REGIONS:
        try:
            regions[region_key] = _analyze_region(region_key)
        except Exception as exc:
            errors.append(f"{region_key}: {type(exc).__name__}: {exc}")
            regions[region_key] = {
                "bolge": REGIONS[region_key]["label"],
                "durum": "hata",
                "hata": f"{type(exc).__name__}: {exc}",
            }

    return {
        "amac": (
            "Son açık Sentinel tarihlerinde SCL=4/5 ile kara olduğu bağımsız olarak kanıtlanan "
            "piksellerde, ana çift ve mevcut eski/yeni bulut-gölge zaman-serisi tamamlamalarından "
            "sonra gözlemsiz kalan alanı ve üretime dokunmadan çoklu uygun yedek sahnelerin "
            "yalnız kapsama bakımından sağlayabileceği ek geri kazanımı ölçmek; alarm veya görev üretmez."
        ),
        "minimum_alan_m2": MIN_HOTSPOT_AREA_M2,
        "kara_referans_sahne_tavani": LAND_REFERENCE_SCENES,
        "coklu_yedek_sahne_tavani": MULTI_FALLBACK_SCENES,
        "bolgeler": regions,
        "hatalar": errors,
    }


def _write_if_changed(core):
    previous_core = None
    if OUTPUT_FILE.exists():
        try:
            previous = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
            if isinstance(previous, dict):
                previous_core = {key: value for key, value in previous.items() if key != "olusturma"}
        except (OSError, ValueError, json.JSONDecodeError):
            previous_core = None

    if previous_core == core:
        print("Sentinel kör alan denetimi değişmedi; JSON yeniden yazılmadı.")
        return False

    payload = {
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        **core,
    }
    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def main():
    core = _core_payload()
    _write_if_changed(core)

    for region_key, row in core["bolgeler"].items():
        if row.get("durum") == "hata":
            print(f"{region_key}: HATA - {row.get('hata')}")
            continue
        print(
            f"{region_key}: {row['kara_referans_sahne_sayisi']} kara referansı; "
            f"ana çift kör %{row['ana_cift_kor_yuzde']:.4f}; "
            f"tek-yedek geri kazanım %{row['zaman_serisiyle_geri_kazanilan_yuzde']:.4f}; "
            f"tek-yedek sonrası körlük %{row['kalan_kor_yuzde']:.4f}; "
            f"{row['coklu_yedek_sahne_sayisi']} uygun yedekte ek kapsama "
            f"%{row['coklu_yedek_ek_geri_kazanilan_yuzde']:.4f}; "
            f"çoklu-yedek sonrası potansiyel körlük %{row['coklu_yedek_sonrasi_kalan_kor_yuzde']:.4f}; "
            f"250m²+ küme {row['kalan_250m2_ustu_kume']}→{row['coklu_yedek_sonrasi_250m2_ustu_kume']}; "
            f"çözümlenmemiş yüzey %{row['cozumlenmemis_yuzey_yuzde']:.4f}"
        )

    if core["hatalar"]:
        raise RuntimeError("Kör alan denetimi tamamlanamadı: " + " | ".join(core["hatalar"]))


if __name__ == "__main__":
    main()
