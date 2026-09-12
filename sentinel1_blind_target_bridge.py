"""Sentinel-1 ayni-geometri ciftini optik kor alan hedefleriyle esler.

Bu katman yalniz diagnostiktir. Sentinel-1 geri-sacilim degisimi tek basina
insaat/kazi kaniti sayilmaz; alarm veya saha gorevi uretmez. Amac, Sentinel-2
bulut/golge nedeniyle goremedigi tarihsel-kara alanlardan hangilerinin guncel
ve karsilastirilabilir Sentinel-1 cifti tarafindan gercekten kapsandigini ve
raster karsilastirmasi icin gerekli polarizasyon assetlerinin bulunup
bulunmadigini olcmektir.

Ana hedef yolu 250 m2 ve ustunde kalir. 150-249 m2 MIKRO katmanindan yalniz
mevcut guclu diagnostik kapilarini gecen veya sahada SANTIYE_KAZI olarak
eslestirilmis adaylar ayri bir kalibrasyon hedefi olarak eklenebilir. Ayrica
250 m2+ sahada dogrulanmis yikim/parsel temizligi noktalari, yeni hafriyat
baslangicini optik yeni sahne beklemeden SAR ile diagnostik izleyebilmek icin
ayri saha-oncul hedef olarak eklenir. Bu eklemeler ana esigi dusurmez ve tek
basina alarm/saha gorevi uretmez.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3

import sentinel1_scene_probe as s1


MIN_MAIN_M2 = 250
MICRO_RANGE_M2 = [150, 249]
MAX_TARGETS_PER_REGION = 8
MAX_MICRO_TARGETS_PER_REGION = 2
MAX_DEMOLITION_TARGETS_PER_REGION = 2
DEMOLITION_MAX_M2 = 5_000
DEMOLITION_MAX_AGE_DAYS = 60
FIELD_DB_FILE = Path(__file__).with_name("santiye.db")


def _safe_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _target(lat, lon, area, source, reason=None, locality=None):
    try:
        lat = float(lat)
        lon = float(lon)
        area = int(round(float(area)))
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or area < MIN_MAIN_M2:
        return None
    return {
        "enlem": round(lat, 6),
        "boylam": round(lon, 6),
        "alan_m2": area,
        "kaynak": source,
        "neden": reason or "OPTIK_KOR_ALAN",
        "mahalle_yaklasik": locality or "Mevki dogrulanmadi",
        "hedef_katmani": "ANA_250_PLUS",
    }


def _micro_target(row):
    """Yalniz guclu/gercek saha destekli MIKRO adayini SAR kalibrasyonuna al."""
    try:
        lat = float(row.get("enlem"))
        lon = float(row.get("boylam"))
        area = int(round(float(row.get("alan_m2"))))
    except (TypeError, ValueError):
        return None

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if not (MICRO_RANGE_M2[0] <= area <= MICRO_RANGE_M2[1]):
        return None

    region_key = str(row.get("bolge") or "").strip().lower()
    if region_key not in {"cesme", "uzunkuyu", "gulbahce"}:
        return None

    field_result = str(row.get("saha_eslesme_sonucu") or "").strip().upper()
    field_confirmed = bool(row.get("manuel_saha_dogrulamasi")) and field_result == "SANTIYE_KAZI"
    strong_diagnostic = bool(row.get("mikro_guclu_diagnostik"))
    if not (field_confirmed or strong_diagnostic):
        return None

    reason = "SAHA_DOGRULANMIS_MIKRO_KALIBRASYON" if field_confirmed else "MIKRO_GUCLU_DIAGNOSTIK"
    target = {
        "enlem": round(lat, 6),
        "boylam": round(lon, 6),
        "alan_m2": area,
        "kaynak": "micro_site_decision_review.json",
        "neden": reason,
        "mahalle_yaklasik": row.get("yaklasik_mevki") or "Mevki dogrulanmadi",
        "hedef_katmani": "MIKRO_DIAGNOSTIK",
        "mikro_guclu_diagnostik": strong_diagnostic,
        "mikro_saha_dogrulandi": field_confirmed,
    }
    return region_key, target


def _append_micro_calibration_targets(result):
    decision = _safe_json("micro_site_decision_review.json")
    grouped = {"cesme": [], "uzunkuyu": [], "gulbahce": []}
    for row in decision.get("adaylar") or []:
        parsed = _micro_target(row)
        if not parsed:
            continue
        region_key, target = parsed
        grouped[region_key].append(target)

    for region_key, rows in grouped.items():
        rows.sort(
            key=lambda row: (
                bool(row.get("mikro_saha_dogrulandi")),
                bool(row.get("mikro_guclu_diagnostik")),
                row.get("alan_m2") or 0,
            ),
            reverse=True,
        )
        existing = {
            (round(float(row["enlem"]), 6), round(float(row["boylam"]), 6))
            for row in result.get(region_key) or []
        }
        added = 0
        for row in rows:
            key = (round(float(row["enlem"]), 6), round(float(row["boylam"]), 6))
            if key in existing:
                continue
            result[region_key].append(row)
            existing.add(key)
            added += 1
            if added >= MAX_MICRO_TARGETS_PER_REGION:
                break
    return result


def _parse_field_date(value):
    text = str(value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _load_demolition_precursors():
    """Saha DB'sinden yikim/temizlik sonucunu salt-okunur diagnostik kanit olarak al."""
    if not FIELD_DB_FILE.exists():
        return []
    connection = None
    try:
        connection = sqlite3.connect(
            f"file:{FIELD_DB_FILE}?mode=ro",
            uri=True,
            timeout=5,
        )
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(saha_sonuclari)")
        }
        required = {"gorev_id", "sonuc", "enlem", "boylam", "alan_m2", "son_tarih"}
        if not required.issubset(columns):
            return []
        rows = connection.execute(
            """SELECT gorev_id,sonuc,enlem,boylam,alan_m2,son_tarih
            FROM saha_sonuclari
            WHERE sonuc = 'YIKIM_TEMIZLIK'
              AND enlem IS NOT NULL AND boylam IS NOT NULL
              AND alan_m2 IS NOT NULL AND son_tarih IS NOT NULL"""
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if connection is not None:
            connection.close()

    return [
        {
            "gorev_id": str(task_id or "").strip(),
            "sonuc": str(outcome or "").strip().upper(),
            "enlem": latitude,
            "boylam": longitude,
            "alan_m2": area,
            "son_tarih": last_date,
        }
        for task_id, outcome, latitude, longitude, area, last_date in rows
    ]


def _point_in_bbox(lat, lon, bbox):
    try:
        west, south, east, north = map(float, bbox)
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return False
    return west <= lon <= east and south <= lat <= north


def _demolition_region(lat, lon):
    """Saha onculunu en dar diagnostik AOI'ye ata; Gulbahce once gelir."""
    for region_key in ("gulbahce", "cesme", "uzunkuyu"):
        aoi = s1.AOIS.get(region_key) or {}
        if _point_in_bbox(lat, lon, aoi.get("bbox") or []):
            return region_key
    return None


def _demolition_target(row, today=None):
    """Taze sahada dogrulanmis 250 m2+ yikimi salt SAR diagnostik hedefe cevir."""
    if not isinstance(row, dict):
        return None
    if str(row.get("sonuc") or "").strip().upper() != "YIKIM_TEMIZLIK":
        return None

    try:
        lat = float(row.get("enlem"))
        lon = float(row.get("boylam"))
        area = int(round(float(row.get("alan_m2"))))
    except (TypeError, ValueError):
        return None
    if not (MIN_MAIN_M2 <= area <= DEMOLITION_MAX_M2):
        return None

    scene_day = _parse_field_date(row.get("son_tarih"))
    if scene_day is None:
        return None
    today = today or date.today()
    age_days = (today - scene_day).days
    if age_days < 0 or age_days > DEMOLITION_MAX_AGE_DAYS:
        return None

    region_key = _demolition_region(lat, lon)
    if not region_key:
        return None
    locality = {
        "gulbahce": "Gulbahce",
        "cesme": "Cesme diagnostik bolgesi",
        "uzunkuyu": "Uzunkuyu genis diagnostik bolgesi",
    }[region_key]
    target = _target(
        lat,
        lon,
        area,
        "santiye.db",
        "SAHA_DOGRULANMIS_YIKIM_ONCULU",
        locality,
    )
    if target is None:
        return None
    target.update(
        {
            "hedef_katmani": "SAHA_ONCUL_SAR_DIAGNOSTIK",
            "saha_dogrulanmis_yikim_onculu": True,
            "saha_oncul_gorev_id": str(row.get("gorev_id") or "").strip(),
            "saha_oncul_son_tarih": scene_day.strftime("%d.%m.%Y"),
            "saha_oncul_yas_gun": age_days,
        }
    )
    return region_key, target


def _append_demolition_precursor_targets(result, field_precursors=None, today=None):
    """Yikim teyidini optik sahne gelmese bile sinirli SAR diagnostigine ekle."""
    if field_precursors is None:
        field_precursors = _load_demolition_precursors()
    grouped = {"cesme": [], "uzunkuyu": [], "gulbahce": []}
    for row in field_precursors or []:
        parsed = _demolition_target(row, today=today)
        if not parsed:
            continue
        region_key, target = parsed
        grouped[region_key].append(target)

    for region_key, rows in grouped.items():
        rows.sort(
            key=lambda row: (
                _parse_field_date(row.get("saha_oncul_son_tarih")) or date.min,
                row.get("alan_m2") or 0,
            ),
            reverse=True,
        )
        existing = {
            (round(float(row["enlem"]), 6), round(float(row["boylam"]), 6)): row
            for row in result.get(region_key) or []
        }
        added = 0
        for row in rows:
            key = (round(float(row["enlem"]), 6), round(float(row["boylam"]), 6))
            if key in existing:
                existing[key].update(
                    {
                        "saha_dogrulanmis_yikim_onculu": True,
                        "saha_oncul_gorev_id": row.get("saha_oncul_gorev_id"),
                        "saha_oncul_son_tarih": row.get("saha_oncul_son_tarih"),
                        "saha_oncul_yas_gun": row.get("saha_oncul_yas_gun"),
                    }
                )
                continue
            result[region_key].append(row)
            existing[key] = row
            added += 1
            if added >= MAX_DEMOLITION_TARGETS_PER_REGION:
                break
    return result


def _load_targets():
    coverage = _safe_json("coverage_blind_area_audit.json")
    regions = coverage.get("bolgeler") or {}
    result = {"cesme": [], "uzunkuyu": [], "gulbahce": []}

    for region_key in ("cesme", "uzunkuyu"):
        rows = ((regions.get(region_key) or {}).get("ornekler") or [])
        parsed = []
        for row in rows:
            item = _target(
                row.get("enlem"),
                row.get("boylam"),
                row.get("alan_m2"),
                "coverage_blind_area_audit.json",
                row.get("neden"),
                row.get("mahalle_yaklasik"),
            )
            if item:
                parsed.append(item)
        parsed.sort(key=lambda row: row["alan_m2"], reverse=True)
        result[region_key] = parsed[:MAX_TARGETS_PER_REGION]

    gul = _safe_json("gulbahce_latest_state_blind_review.json")
    gul_rows = gul.get("guncel_sahne_kor_kumeleri") or []
    parsed_gul = []
    for row in gul_rows:
        item = _target(
            row.get("enlem"),
            row.get("boylam"),
            row.get("alan_m2"),
            "gulbahce_latest_state_blind_review.json",
            row.get("neden"),
            "Gulbahce",
        )
        if item:
            parsed_gul.append(item)
    parsed_gul.sort(key=lambda row: row["alan_m2"], reverse=True)
    result["gulbahce"] = parsed_gul[:MAX_TARGETS_PER_REGION]
    result = _append_micro_calibration_targets(result)
    return _append_demolition_precursor_targets(result)


def _asset_polarization_map(item):
    """Yalniz gercek olcum raster assetlerini polarizasyon olarak kabul et.

    STAC kaydindaki thumbnail/schema dosyalarinin baslik/aciklamalarinda VV/VH
    gibi dizgeler gecse bile bunlar geri-sacilim rasteri degildir. Bu nedenle
    asset anahtarini (ve yalniz veri rolu varsa tam eslesen basligi) metadata'da
    ilan edilen polarizasyonlarla eslestiririz.
    """
    declared = set(s1._polarizations(item))
    found = {}
    for key, asset in (item.get("assets") or {}).items():
        if not isinstance(asset, dict) or not asset.get("href"):
            continue

        key_pol = str(key or "").strip().upper()
        title_pol = str(asset.get("title") or "").strip().upper()
        roles = {str(role).strip().lower() for role in (asset.get("roles") or []) if role}

        pol = None
        if key_pol in declared:
            pol = key_pol
        elif title_pol in declared and "data" in roles:
            pol = title_pol

        if pol:
            found.setdefault(pol, []).append(str(key))

    return {pol: sorted(set(keys)) for pol, keys in sorted(found.items())}


def _common_raster_polarizations(older, newer):
    old_map = _asset_polarization_map(older)
    new_map = _asset_polarization_map(newer)
    declared_common = set(s1._polarizations(older)).intersection(s1._polarizations(newer))
    common = sorted(set(old_map).intersection(new_map).intersection(declared_common))
    return common, old_map, new_map


def _point_on_segment(lon, lat, a, b, eps=1e-10):
    try:
        x1, y1 = float(a[0]), float(a[1])
        x2, y2 = float(b[0]), float(b[1])
    except (TypeError, ValueError, IndexError):
        return False
    cross = (lon - x1) * (y2 - y1) - (lat - y1) * (x2 - x1)
    if abs(cross) > eps:
        return False
    return min(x1, x2) - eps <= lon <= max(x1, x2) + eps and min(y1, y2) - eps <= lat <= max(y1, y2) + eps


def _point_in_ring(lon, lat, ring):
    if not isinstance(ring, (list, tuple)) or len(ring) < 3:
        return False
    inside = False
    points = list(ring)
    for idx, current in enumerate(points):
        previous = points[idx - 1]
        if _point_on_segment(lon, lat, previous, current):
            return True
        try:
            x1, y1 = float(previous[0]), float(previous[1])
            x2, y2 = float(current[0]), float(current[1])
        except (TypeError, ValueError, IndexError):
            continue
        if (y1 > lat) == (y2 > lat):
            continue
        crossing_lon = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
        if lon < crossing_lon:
            inside = not inside
    return inside


def _point_in_polygon(lon, lat, polygon):
    if not isinstance(polygon, (list, tuple)) or not polygon:
        return False
    if not _point_in_ring(lon, lat, polygon[0]):
        return False
    return not any(_point_in_ring(lon, lat, hole) for hole in polygon[1:])


def _point_in_geometry(item, point):
    """STAC footprint varsa bbox yerine gercek Polygon/MultiPolygon'u kullan.

    RTC/GRD bbox'i egik SAR seridinin bos koselerini de kapsayabilir. Bu nedenle
    bbox icindeki bir hedef gercek raster footprint'inin disinda kalabilir ve
    sonradan nodata okuyabilir. Geometry yok/bozuksa eski bbox davranisina geri
    donulur; boylece kaynak metadata eksikligi kapsama kaybi yaratmaz.
    """
    geometry = item.get("geometry")
    if not isinstance(geometry, dict):
        return None
    kind = str(geometry.get("type") or "")
    coordinates = geometry.get("coordinates")
    lat, lon = map(float, point)
    if kind == "Polygon" and isinstance(coordinates, (list, tuple)):
        return _point_in_polygon(lon, lat, coordinates)
    if kind == "MultiPolygon" and isinstance(coordinates, (list, tuple)):
        return any(_point_in_polygon(lon, lat, polygon) for polygon in coordinates)
    return None


def _point_covered(item, point):
    precise = _point_in_geometry(item, point)
    if precise is not None:
        return bool(precise)
    return s1._point_in_item(item, point)


def _target_covered_by_pair(target, older, newer):
    point = (target["enlem"], target["boylam"])
    return _point_covered(older, point) and _point_covered(newer, point)


def inspect_region(region_key, targets, search_fn=s1._search_items):
    aoi = s1.AOIS[region_key]
    search = search_fn(aoi["bbox"])
    if isinstance(search, dict):
        items = search.get("items") or []
        source = search.get("kaynak")
    else:
        items = search or []
        source = "test"

    pair = s1._find_same_geometry_pair(items, region_key)
    if not pair:
        return {
            "bolge": region_key,
            "durum": "KARSILASTIRILABILIR_S1_CIFTI_YOK",
            "metadata_kaynagi": source,
            "hedef_sayisi": len(targets),
            "cift_tarafindan_kapsanan_hedef": 0,
            "raster_karsilastirma_hazir": False,
            "alarm": False,
            "saha_gorevi": False,
        }

    older, newer = pair
    common_pols, old_assets, new_assets = _common_raster_polarizations(older, newer)
    pair_ready = bool(common_pols)

    checked = []
    for target in targets:
        covered = _target_covered_by_pair(target, older, newer)
        row = dict(target)
        row.update(
            {
                "s1_cifti_kapsiyor": bool(covered),
                "sar_raster_karsilastirmasina_uygun": bool(covered and pair_ready),
                "alarm": False,
                "saha_gorevi": False,
            }
        )
        checked.append(row)

    covered_count = sum(1 for row in checked if row["s1_cifti_kapsiyor"])
    ready_count = sum(1 for row in checked if row["sar_raster_karsilastirmasina_uygun"])
    return {
        "bolge": region_key,
        "durum": "HAZIR" if pair_ready else "CIFT_VAR_RASTER_ASSET_EKSIK",
        "metadata_kaynagi": source,
        "eski_item": older.get("id"),
        "eski_tarih": s1._iso_datetime(older).date().isoformat() if s1._iso_datetime(older) else None,
        "yeni_item": newer.get("id"),
        "yeni_tarih": s1._iso_datetime(newer).date().isoformat() if s1._iso_datetime(newer) else None,
        "goreli_yorunge": s1._relative_orbit(newer),
        "orbit_yonu": s1._orbit_state(newer),
        "ortak_metadata_polarizasyonlari": list(s1._polarizations(newer)),
        "ortak_raster_polarizasyonlari": common_pols,
        "eski_raster_assetleri": old_assets,
        "yeni_raster_assetleri": new_assets,
        "hedef_sayisi": len(checked),
        "cift_tarafindan_kapsanan_hedef": covered_count,
        "raster_karsilastirmaya_hazir_hedef": ready_count,
        "raster_karsilastirma_hazir": pair_ready,
        "hedefler": checked,
        "alarm": False,
        "saha_gorevi": False,
    }


def _self_check():
    bbox = s1.AOIS["gulbahce"]["bbox"]

    assert _target(38.2, 26.4, 200, "test") is None
    parsed_micro = _micro_target(
        {
            "bolge": "cesme",
            "yaklasik_mevki": "Ciftlikkoy",
            "enlem": 38.287679,
            "boylam": 26.266305,
            "alan_m2": 200,
            "manuel_saha_dogrulamasi": True,
            "saha_eslesme_sonucu": "SANTIYE_KAZI",
            "mikro_guclu_diagnostik": False,
        }
    )
    assert parsed_micro is not None
    micro_region, micro_row = parsed_micro
    assert micro_region == "cesme"
    assert micro_row["hedef_katmani"] == "MIKRO_DIAGNOSTIK"
    assert micro_row["mikro_saha_dogrulandi"] is True
    assert _micro_target(
        {
            "bolge": "cesme",
            "enlem": 38.28,
            "boylam": 26.26,
            "alan_m2": 200,
            "manuel_saha_dogrulamasi": False,
            "saha_eslesme_sonucu": "",
            "mikro_guclu_diagnostik": False,
        }
    ) is None

    parsed_demolition = _demolition_target(
        {
            "gorev_id": "UTESTYIKIM",
            "sonuc": "YIKIM_TEMIZLIK",
            "enlem": 38.331547,
            "boylam": 26.644338,
            "alan_m2": 400,
            "son_tarih": "08.09.2026",
        },
        today=date(2026, 9, 12),
    )
    assert parsed_demolition is not None
    demolition_region, demolition_row = parsed_demolition
    assert demolition_region == "gulbahce"
    assert demolition_row["hedef_katmani"] == "SAHA_ONCUL_SAR_DIAGNOSTIK"
    assert demolition_row["saha_dogrulanmis_yikim_onculu"] is True
    assert demolition_row["alan_m2"] == 400
    assert _demolition_target(
        {
            "sonuc": "YIKIM_TEMIZLIK",
            "enlem": 38.331547,
            "boylam": 26.644338,
            "alan_m2": 200,
            "son_tarih": "08.09.2026",
        },
        today=date(2026, 9, 12),
    ) is None

    def fake(day, assets=True):
        payload = {
            "id": f"S1_{day}",
            "bbox": list(bbox),
            "properties": {
                "datetime": f"2026-09-{day:02d}T16:14:00Z",
                "sat:relative_orbit": 29,
                "sat:orbit_state": "ascending",
                "sar:polarizations": ["VV", "VH"],
                "sar:instrument_mode": "IW",
            },
        }
        if assets:
            payload["assets"] = {
                "vv": {"href": "https://example.test/vv.tif", "title": "VV", "roles": ["data"]},
                "vh": {"href": "https://example.test/vh.tif", "title": "VH", "roles": ["data"]},
                "thumbnail": {
                    "href": "https://example.test/thumbnail.png",
                    "title": "HH HV VH VV preview",
                    "roles": ["thumbnail"],
                },
                "schema-product-vv": {
                    "href": "https://example.test/product-vv.xml",
                    "title": "VV product schema",
                    "roles": ["metadata"],
                },
            }
        return payload

    targets = [
        {
            "enlem": 38.341406,
            "boylam": 26.643308,
            "alan_m2": 1200,
            "kaynak": "test",
            "neden": "BULUT",
            "mahalle_yaklasik": "Gulbahce",
        }
    ]

    def fake_search(_bbox):
        assert list(_bbox) == list(bbox)
        return {"kaynak": "test", "items": [fake(11), fake(5)]}

    row = inspect_region("gulbahce", targets, search_fn=fake_search)
    assert row["durum"] == "HAZIR"
    assert row["ortak_raster_polarizasyonlari"] == ["VH", "VV"]
    assert row["eski_raster_assetleri"] == {"VH": ["vh"], "VV": ["vv"]}
    assert row["cift_tarafindan_kapsanan_hedef"] == 1
    assert row["raster_karsilastirmaya_hazir_hedef"] == 1
    assert row["alarm"] is False and row["saha_gorevi"] is False

    # Bbox hedefi iceriyor olsa bile gercek SAR footprint'i disinda kalan hedef
    # kapsanmis sayilmamali. Bu, RTC'de sonradan nodata okunan egik-serit
    # koselerini daha erken ayirir.
    geometry_item = fake(11)
    geometry_item["geometry"] = {
        "type": "Polygon",
        "coordinates": [[[26.63, 38.33], [26.65, 38.33], [26.65, 38.34], [26.63, 38.34], [26.63, 38.33]]],
    }
    assert s1._point_in_item(geometry_item, (38.341406, 26.643308)) is True
    assert _point_covered(geometry_item, (38.341406, 26.643308)) is False
    assert _point_covered(geometry_item, (38.335, 26.64)) is True

    def missing_assets(_bbox):
        return {"kaynak": "test", "items": [fake(11, assets=False), fake(5, assets=False)]}

    row = inspect_region("gulbahce", targets, search_fn=missing_assets)
    assert row["durum"] == "CIFT_VAR_RASTER_ASSET_EKSIK"
    assert row["raster_karsilastirmaya_hazir_hedef"] == 0
    print("Sentinel-1 optik kor alan + MIKRO + saha-yikim SAR hedef koprusu oz testi OK.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check-only", action="store_true")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    if args.self_check_only:
        _self_check()
        return

    targets = _load_targets()
    rows = []
    for region_key in ("cesme", "uzunkuyu", "gulbahce"):
        try:
            rows.append(inspect_region(region_key, targets.get(region_key) or []))
        except Exception as exc:  # dis kaynak/metadata hatasi diagnostigi durdurmasin
            rows.append(
                {
                    "bolge": region_key,
                    "durum": "DIAGNOSTIK_HATA",
                    "hata": type(exc).__name__,
                    "hedef_sayisi": len(targets.get(region_key) or []),
                    "alarm": False,
                    "saha_gorevi": False,
                }
            )

    payload = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": MIN_MAIN_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "amac": "Optik kor alanlarini ayni-geometri Sentinel-1 ciftinin gercek footprint ve raster karsilastirma uygunluguyla eslemek; saha-dogrulanmis/guclu 150-249 m2 MIKRO adaylarini ve 250 m2+ dogrulanmis yikim/parsel temizligi oncul noktalarini ayri SAR diagnostik hedefleri olarak izlemek; geri-sacilim degisimini henuz insaat/kazi kaniti saymamak.",
        "bolgeler": rows,
        "toplam_hedef": sum(int(row.get("hedef_sayisi") or 0) for row in rows),
        "sar_raster_karsilastirmaya_hazir_hedef": sum(int(row.get("raster_karsilastirmaya_hazir_hedef") or 0) for row in rows),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
