"""En yeni Sentinel görüntüsündeki bulut/gölge körlüğünü son açık sahneyle tamamlar.

Ana Sentinel motoru en yeni görüntüyü referans alır. En yeni sahnenin belirli bir
sokağı bulut/gölge altında bırakması halinde o piksel bir sonraki açık geçişe kadar
ana değişim maskesine giremez. Bu tamamlayıcı katman eşikleri gevşetmeden yalnız
bu geçici kör bölgelerde bir önceki açık ana sahneyi "son açık kanıt" olarak
kullanır ve 7+ günlük daha eski yedek sahneyle karşılaştırır.

Böylece örneğin 24 Ağustos'ta açık olan bir kazı alanı 26 Ağustos görüntüsünde
bulut altında kaldı diye 29/31 Ağustos'a kadar beklemek zorunda kalmaz. Sonuç,
en yeni tarihte değişim olmuş gibi sunulmaz; kanıtın son açık tarihi açıkça yazılır.
Su, no-data ve doygun piksel daha eski tarihten doldurulmaz.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import numpy as np

from daily_report import ISTANBUL, REPORT_REGIONS, build_daily_report, ensure_daily_schema
from satellite import (
    REGIONS,
    _clean_mask,
    _hotspots,
    _ndvi,
    _output_shape,
    _read_asset,
    _reflectance,
    _search_items,
    sentinel_pair,
)
from scanner import connect
from temporal_gap_scan import (
    EXCLUDED_CLASSES,
    TEMPORAL_ADDITION_LIMIT,
    TEMPORAL_SMALL_QUOTA,
    merge_candidates,
    select_fallback,
)


# Dedupe politikası temporal_gap_scan.merge_candidates ile ortaktır. Sürüm artışı
# mevcut Sentinel sahnesini de 25 m eşiğiyle yeniden değerlendirir; böylece daha
# önce 25-80 m bandında sessizce ezilmiş olası komşu parsel adayları varsa geri gelir.
LATEST_CLOUD_GAP_VERSION = "latest-cloud-gap-v3-dedupe25m"
# PB04.00+ SCL=2 topografik/cast shadow'dur. En yeni sahnedeki 2 de bulut/gölge
# gibi geçici körlük sayılır; yalnız primary ve fallback açık olduğunda geri kazanılır.
TRANSIENT_LATEST_CLASSES = np.array([2, 3, 8, 9, 10, 11])


def _item_time(item):
    return datetime.fromisoformat(item["properties"]["datetime"].replace("Z", "+00:00"))


def _item_date(item):
    return _item_time(item).strftime("%d.%m.%Y")


def latest_gap_zone(primary_scl, latest_scl, fallback_scl):
    """Yalnız en yeni sahnede geçici atmosferik/gölge kapanması olan pikselleri döndürür."""
    primary_valid = ~np.isin(primary_scl, EXCLUDED_CLASSES)
    fallback_valid = ~np.isin(fallback_scl, EXCLUDED_CLASSES)
    latest_transient = np.isin(latest_scl, TRANSIENT_LATEST_CLASSES)
    return latest_transient & primary_valid & fallback_valid


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


def _latest_gap_hotspots(region_key, primary, latest, fallback):
    bbox = REGIONS[region_key]["bbox"]
    height, width = _output_shape(bbox)

    primary_scl = _read_asset(primary, "scl", bbox, height, width, "nearest")[0]
    latest_scl = _read_asset(latest, "scl", bbox, height, width, "nearest")[0]
    fallback_scl = _read_asset(fallback, "scl", bbox, height, width, "nearest")[0]

    gap_zone = latest_gap_zone(primary_scl, latest_scl, fallback_scl)
    gap_pixels = int(gap_zone.sum())
    gap_percent = float(gap_pixels / max(int(latest_scl.size), 1) * 100)
    if gap_pixels < 3:
        return [], gap_pixels, gap_percent

    # En yeni görüntü bu piksellerde kapalıdır. Bu nedenle spektral değişim en yeni
    # görüntüye karşı değil, son açık ana sahneye (primary) karşı ölçülür.
    fallback_visual = _read_asset(
        fallback, "visual", bbox, height, width, "bilinear"
    )[:3]
    primary_visual = _read_asset(
        primary, "visual", bbox, height, width, "bilinear"
    )[:3]
    fallback_red = _reflectance(
        _read_asset(fallback, "red", bbox, height, width, "bilinear")[0]
    )
    primary_red = _reflectance(
        _read_asset(primary, "red", bbox, height, width, "bilinear")[0]
    )
    fallback_nir = _reflectance(
        _read_asset(fallback, "nir", bbox, height, width, "bilinear")[0]
    )
    primary_nir = _reflectance(
        _read_asset(primary, "nir", bbox, height, width, "bilinear")[0]
    )

    old_rgb = np.moveaxis(fallback_visual, 0, 2).astype("float32") / 255
    clear_rgb = np.moveaxis(primary_visual, 0, 2).astype("float32") / 255
    rgb_difference = np.mean(np.abs(clear_rgb - old_rgb), axis=2)
    brightness_gain = np.mean(clear_rgb, axis=2) - np.mean(old_rgb, axis=2)
    older_ndvi = _ndvi(fallback_red, fallback_nir)
    clear_ndvi = _ndvi(primary_red, primary_nir)
    vegetation_loss = older_ndvi - clear_ndvi

    soil_signal = (
        gap_zone
        & (vegetation_loss > 0.14)
        & (brightness_gain > 0.035)
        & (rgb_difference > 0.10)
    )
    strong_visual_change = gap_zone & (rgb_difference > 0.24)
    small_site_signal = (
        gap_zone
        & (vegetation_loss > 0.20)
        & (clear_ndvi < 0.30)
        & (brightness_gain > 0.055)
        & (rgb_difference > 0.14)
    )

    change_mask = _clean_mask(
        soil_signal | strong_visual_change,
        small_site_mask=small_site_signal,
    ) & gap_zone

    hotspots = _hotspots(
        change_mask,
        bbox,
        _pixel_area_m2(bbox, height, width),
        small_site_mask=small_site_signal,
        limit=TEMPORAL_ADDITION_LIMIT,
        small_quota=TEMPORAL_SMALL_QUOTA,
    )

    interval = f"{_item_date(fallback)}→{_item_date(primary)}"
    latest_date = _item_date(latest)
    for item in hotspots:
        original = str(item.get("sinyal") or "Yüzey/toprak değişimi adayı")
        item["sinyal"] = (
            f"Zaman serisi {interval}; en yeni {latest_date} görüntüsünde bulut/gölge, "
            f"son açık kanıt · {original}"
        )
        item["zaman_serisi"] = True
        item["en_yeni_bulut_boslugu"] = True
        item["zaman_serisi_onceki_tarih"] = _item_date(fallback)
        item["zaman_serisi_son_acik_tarih"] = _item_date(primary)
        item["en_yeni_goruntu_tarihi"] = latest_date
    return hotspots, gap_pixels, gap_percent


def _ensure_state_table(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS uydu_son_bulut_boslugu (
        bolge TEXT PRIMARY KEY,
        son_item TEXT NOT NULL,
        surum TEXT NOT NULL,
        ana_onceki_item TEXT,
        yedek_onceki_item TEXT,
        bosluk_piksel INTEGER DEFAULT 0,
        bosluk_yuzde REAL DEFAULT 0,
        eklenen_aday INTEGER DEFAULT 0,
        guncelleme TEXT NOT NULL)"""
    )


def scan_latest_cloud_gaps():
    ensure_daily_schema()
    report_date = datetime.now(ISTANBUL).strftime("%Y-%m-%d")
    changed = []
    skipped = []
    errors = []

    with connect() as connection:
        _ensure_state_table(connection)
        for region_key in REPORT_REGIONS:
            try:
                primary, latest = sentinel_pair(region_key)
                state = connection.execute(
                    "SELECT son_item,surum FROM uydu_son_bulut_boslugu WHERE bolge=?",
                    (region_key,),
                ).fetchone()
                if (
                    state
                    and state[0] == latest["id"]
                    and state[1] == LATEST_CLOUD_GAP_VERSION
                ):
                    skipped.append(region_key)
                    continue

                row = connection.execute(
                    """SELECT hareket_json,hata FROM gunluk_uydu_raporlari
                    WHERE rapor_tarihi=? AND bolge=? AND son_item=? LIMIT 1""",
                    (report_date, region_key, latest["id"]),
                ).fetchone()
                if not row or row[1]:
                    skipped.append(region_key)
                    continue

                items = _search_items(REGIONS[region_key]["bbox"])
                fallback = select_fallback(
                    items,
                    latest,
                    primary,
                    bbox=REGIONS[region_key]["bbox"],
                )
                if fallback is None:
                    skipped.append(region_key)
                    continue

                try:
                    existing = json.loads(row[0] or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    existing = []
                if not isinstance(existing, list):
                    existing = []

                recovered, gap_pixels, gap_percent = _latest_gap_hotspots(
                    region_key, primary, latest, fallback
                )
                merged, additions = merge_candidates(existing, recovered)
                if additions:
                    connection.execute(
                        """UPDATE gunluk_uydu_raporlari SET hareket_json=?
                        WHERE rapor_tarihi=? AND bolge=? AND son_item=?""",
                        (
                            json.dumps(merged, ensure_ascii=False),
                            report_date,
                            region_key,
                            latest["id"],
                        ),
                    )
                    changed.append((region_key, len(additions), gap_percent))

                connection.execute(
                    """INSERT INTO uydu_son_bulut_boslugu
                    (bolge,son_item,surum,ana_onceki_item,yedek_onceki_item,
                    bosluk_piksel,bosluk_yuzde,eklenen_aday,guncelleme)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(bolge) DO UPDATE SET
                    son_item=excluded.son_item,surum=excluded.surum,
                    ana_onceki_item=excluded.ana_onceki_item,
                    yedek_onceki_item=excluded.yedek_onceki_item,
                    bosluk_piksel=excluded.bosluk_piksel,
                    bosluk_yuzde=excluded.bosluk_yuzde,
                    eklenen_aday=excluded.eklenen_aday,
                    guncelleme=excluded.guncelleme""",
                    (
                        region_key,
                        latest["id"],
                        LATEST_CLOUD_GAP_VERSION,
                        primary["id"],
                        fallback["id"],
                        gap_pixels,
                        gap_percent,
                        len(additions),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            except Exception as exc:
                errors.append(f"{region_key}: {type(exc).__name__}: {exc}")

    if changed:
        build_daily_report()
    return changed, skipped, errors


if __name__ == "__main__":
    changed, skipped, errors = scan_latest_cloud_gaps()
    if changed:
        detail = ", ".join(
            f"{region}=+{count} aday (en-yeni bulut/gölge kör alanı %{gap:.2f})"
            for region, count, gap in changed
        )
        print("En yeni görüntü bulut-körlük tamamlama: " + detail)
    else:
        print("En yeni görüntü bulut/gölge körlüğünden yeni saha adayı eklenmedi.")
    if skipped:
        print("Atlanan/güncel bölgeler: " + ", ".join(skipped))
    if errors:
        print("En yeni görüntü bulut-körlük geçici hataları: " + " | ".join(errors))
