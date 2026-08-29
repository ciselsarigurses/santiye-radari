"""Sentinel zaman serisinde bulut/gölge nedeniyle oluşan eski-görüntü körlüklerini tamamlar.

Ana değişim motorunun eşiklerini gevşetmez. En yeni görüntü açıkken, ana karşılaştırma
olarak seçilen eski görüntünün yalnız bulut/gölge sınıflarında geçersiz kaldığı
pikselleri daha eski ve kullanılabilir bir Sentinel sahnesiyle ikinci kez değerlendirir.
Böylece hafriyat ana eski görüntüde bulut altında kaldı diye sonraki geçişte kalıcı
olarak kaçmaz. Ek adaylar aynı 250 m² ve güçlü küçük-saha filtrelerinden geçer.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import numpy as np

from daily_report import ISTANBUL, REPORT_REGIONS, build_daily_report, ensure_daily_schema
from satellite import (
    MIN_HOTSPOT_AREA_M2,
    REGIONS,
    SMALL_HOTSPOT_MAX_M2,
    _clean_mask,
    _hotspots,
    _item_covers_bbox,
    _ndvi,
    _output_shape,
    _read_asset,
    _reflectance,
    _relative_orbit,
    _search_items,
    sentinel_pair,
)
from scanner import connect


TEMPORAL_VERSION = "cloud-shadow-gap-v2-full-cover-same-orbit"
FALLBACK_MIN_GAP_DAYS = 7
TEMPORAL_ADDITION_LIMIT = 6
TEMPORAL_SMALL_QUOTA = 3
DUPLICATE_METERS = 80

# Ana eski görüntüde yalnız geçici atmosferik sorunlar için daha eski sahneye düş.
# Su/no-data/doygun pikseli başka tarihten doldurmak kıyı ve granül sınırı kaynaklı
# yanlış pozitif üretebileceği için özellikle kapsam dışı bırakılır.
TRANSIENT_OLDER_CLASSES = np.array([3, 8, 9, 10, 11])
EXCLUDED_CLASSES = np.array([0, 1, 3, 6, 8, 9, 10, 11])


def _item_time(item):
    return datetime.fromisoformat(item["properties"]["datetime"].replace("Z", "+00:00"))


def _item_date(item):
    return _item_time(item).strftime("%d.%m.%Y")


def _same_tile(first, second):
    first_tile = first.get("properties", {}).get("s2:mgrs_tile")
    second_tile = second.get("properties", {}).get("s2:mgrs_tile")
    if first_tile and second_tile:
        return first_tile == second_tile
    return True


def select_fallback(
    items,
    latest,
    primary,
    minimum_gap_days=FALLBACK_MIN_GAP_DAYS,
    bbox=None,
):
    """7+ günlük, tam-kapsam aynı-karo yedeği seç; aynı yörüngeyi tercih et.

    Zaman-serisi katmanı ana motorun bulut/gölge boşluğunu tamamladığı için kısmi
    bir STAC karosunu yedek kabul etmek sessiz kör alan yaratabilir. Farklı göreli
    yörünge de bina kenarı/paralaks farkını gerçek zemin değişimi gibi gösterebilir.
    Bu nedenle yedek sahne analiz kutusunu bütünüyle örtmeli; aynı göreli yörünge
    mevcutsa daha yeni farklı-yörünge sahnesinin önüne geçmelidir.
    """
    latest_time = _item_time(latest)
    candidates = []
    for item in items:
        if item.get("id") in {latest.get("id"), primary.get("id")}:
            continue
        if not _same_tile(item, latest):
            continue
        if bbox is not None and not _item_covers_bbox(item, bbox):
            continue
        age_days = (latest_time - _item_time(item)).total_seconds() / 86400
        if age_days < minimum_gap_days:
            continue
        candidates.append(item)

    latest_orbit = _relative_orbit(latest)
    if latest_orbit is not None:
        same_orbit = [
            item for item in candidates if _relative_orbit(item) == latest_orbit
        ]
        if same_orbit:
            return same_orbit[0]
    return candidates[0] if candidates else None


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _select_additions(candidates, limit=TEMPORAL_ADDITION_LIMIT):
    """Bulut boşluğu adaylarını sınırlarken küçük güçlü şantiyeleri tamamen gömme."""
    ranked = sorted(candidates, key=lambda item: float(item.get("alan_m2") or 0), reverse=True)
    if len(ranked) <= limit:
        return ranked
    small = [item for item in ranked if float(item.get("alan_m2") or 0) <= SMALL_HOTSPOT_MAX_M2]
    standard = [item for item in ranked if float(item.get("alan_m2") or 0) > SMALL_HOTSPOT_MAX_M2]
    selected = standard[: max(limit - TEMPORAL_SMALL_QUOTA, 0)]
    selected.extend(small[:TEMPORAL_SMALL_QUOTA])
    if len(selected) < limit:
        leftovers = [item for item in ranked if item not in selected]
        selected.extend(leftovers[: limit - len(selected)])
    return selected[:limit]


def merge_candidates(existing, recovered):
    """Mevcut adayı korur; 80 m içindeki zaman-serisi sonucunu ikinci görev yapmaz."""
    merged = [item for item in existing if isinstance(item, dict)]
    additions = []
    for item in _select_additions([x for x in recovered if isinstance(x, dict)]):
        latitude = float(item.get("enlem"))
        longitude = float(item.get("boylam"))
        duplicate = False
        for old in merged:
            try:
                old_point = (float(old.get("enlem")), float(old.get("boylam")))
            except (TypeError, ValueError):
                continue
            if _distance_m((latitude, longitude), old_point) < DUPLICATE_METERS:
                duplicate = True
                break
        if duplicate:
            continue
        merged.append(item)
        additions.append(item)
    return merged, additions


def _gap_hotspots(region_key, primary, latest, fallback):
    bbox = REGIONS[region_key]["bbox"]
    height, width = _output_shape(bbox)

    primary_scl = _read_asset(primary, "scl", bbox, height, width, "nearest")[0]
    latest_scl = _read_asset(latest, "scl", bbox, height, width, "nearest")[0]
    fallback_scl = _read_asset(fallback, "scl", bbox, height, width, "nearest")[0]

    latest_valid = ~np.isin(latest_scl, EXCLUDED_CLASSES)
    fallback_valid = ~np.isin(fallback_scl, EXCLUDED_CLASSES)
    transient_gap = np.isin(primary_scl, TRANSIENT_OLDER_CLASSES)
    gap_zone = transient_gap & latest_valid & fallback_valid

    valid_latest_pixels = max(int(latest_valid.sum()), 1)
    gap_pixels = int(gap_zone.sum())
    gap_percent = float(gap_pixels / valid_latest_pixels * 100)
    if gap_pixels < 3:
        return [], gap_pixels, gap_percent

    fallback_visual = _read_asset(fallback, "visual", bbox, height, width, "bilinear")[:3]
    latest_visual = _read_asset(latest, "visual", bbox, height, width, "bilinear")[:3]
    fallback_red = _reflectance(
        _read_asset(fallback, "red", bbox, height, width, "bilinear")[0]
    )
    latest_red = _reflectance(
        _read_asset(latest, "red", bbox, height, width, "bilinear")[0]
    )
    fallback_nir = _reflectance(
        _read_asset(fallback, "nir", bbox, height, width, "bilinear")[0]
    )
    latest_nir = _reflectance(
        _read_asset(latest, "nir", bbox, height, width, "bilinear")[0]
    )

    old_rgb = np.moveaxis(fallback_visual, 0, 2).astype("float32") / 255
    new_rgb = np.moveaxis(latest_visual, 0, 2).astype("float32") / 255
    rgb_difference = np.mean(np.abs(new_rgb - old_rgb), axis=2)
    brightness_gain = np.mean(new_rgb, axis=2) - np.mean(old_rgb, axis=2)
    older_ndvi = _ndvi(fallback_red, fallback_nir)
    latest_ndvi = _ndvi(latest_red, latest_nir)
    vegetation_loss = older_ndvi - latest_ndvi

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
        & (latest_ndvi < 0.30)
        & (brightness_gain > 0.055)
        & (rgb_difference > 0.14)
    )

    change_mask = _clean_mask(
        soil_signal | strong_visual_change,
        small_site_mask=small_site_signal,
    ) & gap_zone

    west, south, east, north = bbox
    pixel_width_m = (
        (east - west) * 111320 * np.cos(np.radians((south + north) / 2)) / width
    )
    pixel_height_m = (north - south) * 110570 / height
    pixel_area_m2 = pixel_width_m * pixel_height_m

    hotspots = _hotspots(
        change_mask,
        bbox,
        pixel_area_m2,
        small_site_mask=small_site_signal,
        limit=TEMPORAL_ADDITION_LIMIT,
        small_quota=TEMPORAL_SMALL_QUOTA,
    )
    interval = f"{_item_date(fallback)}→{_item_date(latest)}"
    for item in hotspots:
        original = str(item.get("sinyal") or "Yüzey/toprak değişimi adayı")
        item["sinyal"] = (
            f"Zaman serisi {interval}; ana eski görüntüde bulut/gölge boşluğu · {original}"
        )
        item["zaman_serisi"] = True
        item["zaman_serisi_onceki_tarih"] = _item_date(fallback)
    return hotspots, gap_pixels, gap_percent


def _ensure_state_table(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS uydu_zaman_serisi (
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


def scan_temporal_gaps():
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
                    "SELECT son_item,surum FROM uydu_zaman_serisi WHERE bolge=?",
                    (region_key,),
                ).fetchone()
                if state and state[0] == latest["id"] and state[1] == TEMPORAL_VERSION:
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

                recovered, gap_pixels, gap_percent = _gap_hotspots(
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
                    """INSERT INTO uydu_zaman_serisi
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
                        TEMPORAL_VERSION,
                        primary["id"],
                        fallback["id"],
                        gap_pixels,
                        gap_percent,
                        len(additions),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            except Exception as exc:
                # Bu katman tamamlayıcıdır. Geçici STAC/COG hatası ana günlük radarı
                # düşürmez ve durum yazılmadığı için sonraki çalışmada yeniden denenir.
                errors.append(f"{region_key}: {type(exc).__name__}: {exc}")

    if changed:
        # DB hareket listesi güncellendi; kullanıcıya açık JSON/Markdown raporunu aynı
        # son_item satırından yeniden üret. build_daily_report mevcut satırı tekrar
        # analiz etmez, yalnız güncel hareket_json'u yansıtır.
        build_daily_report()
    return changed, skipped, errors


if __name__ == "__main__":
    changed, skipped, errors = scan_temporal_gaps()
    if changed:
        detail = ", ".join(
            f"{region}=+{count} aday (eski-görüntü bulut/gölge boşluğu %{gap:.2f})"
            for region, count, gap in changed
        )
        print("Zaman serisi körlük tamamlama: " + detail)
    else:
        print("Zaman serisi körlük tamamlamada yeni saha adayı eklenmedi.")
    if skipped:
        print("Atlanan/güncel bölgeler: " + ", ".join(skipped))
    if errors:
        print("Zaman serisi geçici hataları: " + " | ".join(errors))
