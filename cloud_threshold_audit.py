"""Sentinel granül bulut eşiğinin AOI içinde taze açık sahne gizleyip gizlemediğini ölçer.

Earth Search ``eo:cloud_cover`` değeri bütün Sentinel granülü içindir; Çeşme gibi
granülün küçük bir bölümünü izleyen bir AOI'de global bulut oranı yüksek olsa bile
ilçe açık olabilir. Üretim motoru şimdilik güvenli ``<25%`` arama eşiğini korur.
Bu denetim eşiği gevşetmeden daha geniş metadata araması yapar ve üretimde seçilen
sahneden daha yeni, tam-kapsam ve karşılaştırılabilir sahneler varsa yalnız SCL
bandını kaba ölçekte okuyarak AOI içindeki gerçek geçici kapanma oranını ölçer.

Gülbahçe, Uzunkuyu üretim kutusunun içinde olsa da ayrı bir 2 km operasyonel pencere
olarak ayrıca denetlenir. Böylece Uzunkuyu kutusunun genel bulut oranı Gülbahçe'de
yerel olarak açık, daha yeni bir sahneyi görünmez kılmaz.

Amaç alarm üretmek değil, ``global bulut >=25%`` filtresinin erken hafriyat kanıtını
sessizce geciktirebildiği durumları sayısal ve makine-okunur biçimde görünür
kılmaktır. Böyle bir sahne bulunursa üretim seçimi ayrıca doğrulanmadan otomatik
değiştirilmez.
"""

from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path

import numpy as np

import satellite


PRODUCTION_MAX_CLOUD = 25
AUDIT_MAX_CLOUD = 100
LOCAL_BLOCKED_MAX_PERCENT = 25.0
AUDIT_PIXEL_SIZE_M = 40
AUDIT_MAX_DIMENSION = 800
OUTPUT_FILE = Path(__file__).with_name("cloud_threshold_audit.json")

# Gülbahçe operasyonel referansıdır; idari/kadastral sınır veya ada-parsel değildir.
GULBAHCE_LAT = 38.33278
GULBAHCE_LON = 26.64556
GULBAHCE_AUDIT_RADIUS_M = 2_000

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


def _rows_newest_first(timed_rows):
    """DD.MM.YYYY metnini değil gerçek UTC zamanı sıralama anahtarı yapar."""
    return [
        row
        for _, row in sorted(
            timed_rows,
            key=lambda pair: pair[0],
            reverse=True,
        )
    ]


def _square_bbox(latitude, longitude, radius_m):
    """Yerel kör-alan denetimi için yaklaşık kare bbox üretir."""
    lat_delta = float(radius_m) / 110_570.0
    lon_scale = 111_320.0 * math.cos(math.radians(float(latitude)))
    if lon_scale <= 0:
        raise ValueError("Geçersiz enlem")
    lon_delta = float(radius_m) / lon_scale
    return [
        float(longitude) - lon_delta,
        float(latitude) - lat_delta,
        float(longitude) + lon_delta,
        float(latitude) + lat_delta,
    ]


def _audit_targets():
    return [
        {
            "key": "cesme",
            "label": "Çeşme üretim kutusu",
            "production_region": "cesme",
            "scope": "uretim_bbox",
            "bbox": list(map(float, satellite.REGIONS["cesme"]["bbox"])),
        },
        {
            "key": "uzunkuyu",
            "label": "Uzunkuyu · Germiyan · Ildır · Gülbahçe üretim kutusu",
            "production_region": "uzunkuyu",
            "scope": "uretim_bbox",
            "bbox": list(map(float, satellite.REGIONS["uzunkuyu"]["bbox"])),
        },
        {
            "key": "gulbahce_2km",
            "label": "Gülbahçe 2 km operasyonel kör-alan penceresi",
            "production_region": "uzunkuyu",
            "scope": "gulbahce_2km_operasyonel",
            "bbox": _square_bbox(
                GULBAHCE_LAT,
                GULBAHCE_LON,
                GULBAHCE_AUDIT_RADIUS_M,
            ),
        },
    ]


def audit_target(target):
    bbox = target["bbox"]
    region_key = target["production_region"]
    _, production_latest = satellite.sentinel_pair(region_key)
    production_time = _item_time(production_latest)
    broad_items = satellite._search_items(bbox, max_cloud=AUDIT_MAX_CLOUD)

    newer_timed = []
    for item in broad_items:
        item_time = _item_time(item)
        if item_time <= production_time:
            continue
        if not satellite._item_covers_bbox(item, bbox):
            continue
        # Üretim filtresinden zaten geçebilecek sahne burada körlük kanıtı değildir.
        if _global_cloud(item) < PRODUCTION_MAX_CLOUD:
            continue
        if not _can_anchor_pair(item, broad_items, bbox):
            continue
        local_blocked = _local_blocked_percent(item, bbox)
        newer_timed.append(
            (
                item_time,
                {
                    "item": str(item.get("id") or "-"),
                    "tarih": item_time.strftime("%d.%m.%Y %H:%MZ"),
                    "global_bulut": round(_global_cloud(item), 2),
                    "aoi_kapali": round(local_blocked, 2),
                    "aoi_yeterince_acik": local_blocked <= LOCAL_BLOCKED_MAX_PERCENT,
                },
            )
        )

    newer = _rows_newest_first(newer_timed)
    return {
        "hedef": target["key"],
        "etiket": target["label"],
        "uretim_bolgesi": region_key,
        "kapsam": target["scope"],
        "bbox": [round(float(value), 6) for value in bbox],
        "production_item": str(production_latest.get("id") or "-"),
        "production_date": production_time.strftime("%d.%m.%Y %H:%MZ"),
        "newer_candidates": newer,
    }


def audit_region(region_key):
    """Eski çağrılar için üretim-kutusu denetimini korur."""
    target = next(
        target
        for target in _audit_targets()
        if target["key"] == region_key
    )
    return audit_target(target)


def _write_if_changed(payload):
    previous = None
    if OUTPUT_FILE.exists():
        try:
            previous = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            previous = None

    if previous == payload:
        print("Bulut eşiği audit sonucu değişmedi; JSON yeniden yazılmadı.")
        return False

    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def _self_check():
    clear = np.full((10, 10), 4, dtype="uint8")
    clear[0, :] = 9
    assert abs(_blocked_percent(clear) - 10.0) < 1e-9

    # Su pikselleri global-bulut/AOI-kapanma denetiminde bulut sayılmamalı.
    water = np.full((10, 10), 6, dtype="uint8")
    assert _blocked_percent(water) == 0.0

    shadow = np.full((10, 10), 2, dtype="uint8")
    assert _blocked_percent(shadow) == 100.0

    # İnsan-okur tarih metni DD.MM.YYYY olduğundan ay değişiminde leksikografik
    # sıralama yanlış sonuç verir (31.08, 01.09'un önüne geçer). Gerçek zaman anahtarı
    # Ağustos→Eylül sınırında özellikle korunmalı.
    aug31 = datetime.fromisoformat("2026-08-31T09:00:00+00:00")
    sep01 = datetime.fromisoformat("2026-09-01T09:00:00+00:00")
    rows = _rows_newest_first([(aug31, {"id": "old"}), (sep01, {"id": "new"})])
    assert [row["id"] for row in rows] == ["new", "old"]

    # Gülbahçe yerel audit penceresi üretim kutusundan taşmamalı. Bu geometrik
    # invariant değişirse ayrı kapsama guardı da dikkat üretir.
    local_bbox = _square_bbox(
        GULBAHCE_LAT,
        GULBAHCE_LON,
        GULBAHCE_AUDIT_RADIUS_M,
    )
    west, south, east, north = map(float, satellite.REGIONS["uzunkuyu"]["bbox"])
    assert west <= local_bbox[0] < local_bbox[2] <= east
    assert south <= local_bbox[1] < local_bbox[3] <= north
    assert len(_audit_targets()) == 3


def main():
    _self_check()
    blind_spots = []
    warnings = []
    summaries = []
    results = []

    for target in _audit_targets():
        try:
            result = audit_target(target)
        except Exception as exc:
            warnings.append(
                f"{target['key']}: {type(exc).__name__}: {exc}"
            )
            continue

        results.append(result)
        candidates = result["newer_candidates"]
        actionable = [row for row in candidates if row["aoi_yeterince_acik"]]
        blind_spots.extend((target["key"], row) for row in actionable)
        if actionable:
            detail = ", ".join(
                f"{row['item']} global %{row['global_bulut']:.1f} / "
                f"AOI kapalı %{row['aoi_kapali']:.1f}"
                for row in actionable
            )
            summaries.append(
                f"{target['key']}: DAHA YENİ AÇIK AOI ADAYI → {detail}"
            )
        elif candidates:
            summaries.append(
                f"{target['key']}: {len(candidates)} daha yeni global-bulutlu sahne var; "
                "AOI içi kapanma da yüksek, üretim eşiği şimdilik güvenli"
            )
        else:
            summaries.append(
                f"{target['key']}: üretim sahnesinden daha yeni ve global bulut eşiğine "
                "takılan karşılaştırılabilir sahne yok"
            )

    payload = {
        "amac": (
            "Global Sentinel bulut metadata filtresinin Çeşme, Uzunkuyu ve ayrıca "
            "Gülbahçe 2 km operasyonel penceresinde yerel olarak açık daha yeni "
            "sahneleri geciktirip geciktirmediğini ölçmek."
        ),
        "uretim_global_bulut_esigi_yuzde": PRODUCTION_MAX_CLOUD,
        "yerel_kapali_esik_yuzde": LOCAL_BLOCKED_MAX_PERCENT,
        "audit_piksel_m": AUDIT_PIXEL_SIZE_M,
        "alarm_uretmez": True,
        "saha_gorevi_uretmez": True,
        "dikkat_hedef_aday_sayisi": len(blind_spots),
        "uyarilar": warnings,
        "hedefler": results,
    }
    _write_if_changed(payload)

    print("Sentinel global-bulut körlük denetimi: " + " | ".join(summaries))
    if blind_spots:
        print(
            "DİKKAT: global eo:cloud_cover filtresi AOI içinde kullanılabilir daha yeni "
            "Sentinel sahnesini geciktiriyor olabilir; üretim seçimi değiştirilmeden "
            "önce bu sahne ayrıca doğrulanmalı."
        )
    if warnings:
        # Bu katman üretim alarmı üretmeyen tanısal bir denetimdir. Geçici STAC/COG
        # erişim hatası ana günlük taramayı düşürmesin; uyarı logda ve JSON'da görünür kalsın.
        print("Bulut eşiği denetim uyarıları: " + " | ".join(warnings))


if __name__ == "__main__":
    main()
