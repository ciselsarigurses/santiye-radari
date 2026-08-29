"""Sentinel granül bulut eşiğinin AOI içinde taze açık sahne gizleyip gizlemediğini ölçer.

Earth Search ``eo:cloud_cover`` değeri bütün Sentinel granülü içindir; Çeşme gibi
granülün küçük bir bölümünü izleyen bir AOI'de global bulut oranı yüksek olsa bile
ilçe açık olabilir. Üretim motoru şimdilik güvenli ``<25%`` arama eşiğini korur.
Bu denetim eşiği gevşetmeden daha geniş metadata araması yapar ve üretimde seçilen
sahneden daha yeni, tam-kapsam ve karşılaştırılabilir sahneler varsa yalnız SCL
bandını kaba ölçekte okuyarak AOI içindeki gerçek geçici kapanma oranını ölçer.

Amaç alarm üretmek değil, ``global bulut >25%`` filtresinin erken hafriyat kanıtını
sessizce geciktirebildiği durumları sayısal olarak görünür kılmaktır. Böyle bir
sahne bulunursa üretim seçimi ayrıca doğrulanmadan otomatik değiştirilmez.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

import satellite


PRODUCTION_MAX_CLOUD = 25
AUDIT_MAX_CLOUD = 100
LOCAL_BLOCKED_MAX_PERCENT = 25.0
AUDIT_PIXEL_SIZE_M = 40
AUDIT_MAX_DIMENSION = 800
# SCL 2 PB04.00+ için cast/topografik gölge; 3 bulut gölgesi; 8-10 bulut/cirrus,
# 11 kar/buz. 0/1 no-data/doygun piksel de AOI'nin o anda kullanılabilir kanıtı
# sayılmaz. Su (6) burada "bulut" değildir; değişim motoru onu ayrıca dışlar.
LOCAL_BLOCKED_CLASSES = np.array([0, 1, 2, 3, 8, 9, 10, 11], dtype="uint8")


def _item_time(item):
    return datetime.fromisoformat(
        item["properties"]["datetime"].replace("Z", "+00:00")
    )


def _global_cloud(item):
    try:
        return float(item.get("properties", {}).get("eo:cloud_cover", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _blocked_percent(scl):
    array = np.asarray(scl, dtype="uint8")
    if not array.size:
        return 100.0
    return float(np.isin(array, LOCAL_BLOCKED_CLASSES).mean() * 100)


def _local_blocked_percent(item, bbox):
    height, width = satellite._output_shape(
        bbox,
        target_pixel_m=AUDIT_PIXEL_SIZE_M,
        max_dimension=AUDIT_MAX_DIMENSION,
    )
    scl = satellite._read_asset(
        item,
        "scl",
        bbox,
        height,
        width,
        "nearest",
    )[0]
    return _blocked_percent(scl)


def _can_anchor_pair(candidate, broad_items, bbox):
    """Adayın tam-kapsam eski bir referansla gerçekten karşılaştırılabildiğini doğrular."""
    candidate_time = _item_time(candidate)
    older = [item for item in broad_items if _item_time(item) < candidate_time]
    try:
        _, latest = satellite._pick_pair([candidate, *older], bbox=bbox)
    except satellite.SatelliteError:
        return False
    return latest.get("id") == candidate.get("id")


def audit_region(region_key):
    bbox = satellite.REGIONS[region_key]["bbox"]
    _, production_latest = satellite.sentinel_pair(region_key)
    production_time = _item_time(production_latest)
    broad_items = satellite._search_items(bbox, max_cloud=AUDIT_MAX_CLOUD)

    newer = []
    for item in broad_items:
        if _item_time(item) <= production_time:
            continue
        if not satellite._item_covers_bbox(item, bbox):
            continue
        # Üretim filtresinden zaten geçebilecek sahne burada körlük kanıtı değildir.
        if _global_cloud(item) < PRODUCTION_MAX_CLOUD:
            continue
        if not _can_anchor_pair(item, broad_items, bbox):
            continue
        local_blocked = _local_blocked_percent(item, bbox)
        newer.append(
            {
                "item": str(item.get("id") or "-"),
                "tarih": _item_time(item).strftime("%d.%m.%Y %H:%MZ"),
                "global_bulut": round(_global_cloud(item), 2),
                "aoi_kapali": round(local_blocked, 2),
                "aoi_yeterince_acik": local_blocked <= LOCAL_BLOCKED_MAX_PERCENT,
            }
        )

    newer.sort(key=lambda row: row["tarih"], reverse=True)
    return {
        "region": region_key,
        "production_item": str(production_latest.get("id") or "-"),
        "production_date": production_time.strftime("%d.%m.%Y %H:%MZ"),
        "newer_candidates": newer,
    }


def _self_check():
    clear = np.full((10, 10), 4, dtype="uint8")
    clear[0, :] = 9
    assert abs(_blocked_percent(clear) - 10.0) < 1e-9

    # Su pikselleri global-bulut/AOI-kapanma denetiminde bulut sayılmamalı.
    water = np.full((10, 10), 6, dtype="uint8")
    assert _blocked_percent(water) == 0.0

    shadow = np.full((10, 10), 2, dtype="uint8")
    assert _blocked_percent(shadow) == 100.0


def main():
    _self_check()
    blind_spots = []
    warnings = []
    summaries = []

    for region_key in ("cesme", "uzunkuyu"):
        try:
            result = audit_region(region_key)
        except Exception as exc:
            warnings.append(f"{region_key}: {type(exc).__name__}: {exc}")
            continue

        candidates = result["newer_candidates"]
        actionable = [row for row in candidates if row["aoi_yeterince_acik"]]
        blind_spots.extend((region_key, row) for row in actionable)
        if actionable:
            detail = ", ".join(
                f"{row['item']} global %{row['global_bulut']:.1f} / AOI kapalı %{row['aoi_kapali']:.1f}"
                for row in actionable
            )
            summaries.append(f"{region_key}: DAHA YENİ AÇIK AOI ADAYI → {detail}")
        elif candidates:
            summaries.append(
                f"{region_key}: {len(candidates)} daha yeni global-bulutlu sahne var; "
                "AOI içi kapanma da yüksek, üretim eşiği şimdilik güvenli"
            )
        else:
            summaries.append(
                f"{region_key}: üretim sahnesinden daha yeni ve global bulut eşiğine takılan "
                "karşılaştırılabilir sahne yok"
            )

    print("Sentinel global-bulut körlük denetimi: " + " | ".join(summaries))
    if blind_spots:
        print(
            "DİKKAT: global eo:cloud_cover filtresi AOI içinde kullanılabilir daha yeni "
            "Sentinel sahnesini geciktiriyor olabilir; üretim seçimi değiştirilmeden "
            "önce bu sahne ayrıca doğrulanmalı."
        )
    if warnings:
        # Bu katman üretim alarmı üretmeyen tanısal bir denetimdir. Geçici STAC/COG
        # erişim hatası ana günlük taramayı düşürmesin; uyarı logda görünür kalsın.
        print("Bulut eşiği denetim uyarıları: " + " | ".join(warnings))


if __name__ == "__main__":
    main()
