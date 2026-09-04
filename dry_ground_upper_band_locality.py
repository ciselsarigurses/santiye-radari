"""901-2.000 m² kuru-zemin adaylarında üst-bant yerellik diagnostik testi.

Mevcut ``dry_ground_upper_band_watch`` güçlü temporal/izole adayları görünür kılar,
ancak 250-900 m² küçük-saha katmanındaki 5x5 yerellik testi 900 m² üstüne bilinçli
olarak uygulanmaz. Bu dosya o boşluğu üretime dokunmadan ölçer.

Üst bantta adayın 3x3 merkez değişimi, 7x7 pencerenin dış halkasıyla (merkez 5x5
hariç) karşılaştırılır. Böylece yaklaşık 1.000-2.000 m² kompakt bir müdahalenin
hemen bitişik pikselleri çevre referansını gereksiz yere kirletmez. Sonuç yalnız
diagnostiktir; alarm/görev üretmez ve 250 m² ana eşik değişmez.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

import dry_ground_temporal_audit as temporal
import satellite
from daily_report import ISTANBUL


BASE = Path(__file__).resolve().parent
WATCH_JSON = BASE / "dry_ground_upper_band_watch.json"
TEMPORAL_JSON = BASE / "dry_ground_temporal_audit.json"
OUTPUT_JSON = BASE / "dry_ground_upper_band_locality.json"

LOWER_EXCLUSIVE_M2 = 900
UPPER_INCLUSIVE_M2 = 2_000
OUTER_RADIUS_PIXELS = 3
EXCLUDED_CENTER_RADIUS_PIXELS = 2
MIN_RING_VALID_FRACTION = 2 / 3
LOCAL_CONTRAST_MIN = 1.5
MIN_INNER_BSI_CHANGE = 0.10
WIDESPREAD_RING_ABS_MIN = 0.10
WIDESPREAD_RING_RELATIVE_MIN = 0.80

GULBAHCE_LAT = 38.33278
GULBAHCE_LON = 26.64556
GULBAHCE_RADIUS_M = 2_000


def _load(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _distance_m(lat1, lon1, lat2, lon2):
    mean_lat = math.radians((float(lat1) + float(lat2)) / 2.0)
    north = (float(lat1) - float(lat2)) * 110_570.0
    east = (float(lon1) - float(lon2)) * 111_320.0 * math.cos(mean_lat)
    return math.hypot(north, east)


def _upper_ring_means(previous_delta, current_delta, valid_mask, row, column):
    """7x7 pencerenin merkez 5x5 dışındaki halkasını ölç."""
    height, width = valid_mask.shape
    r0 = max(0, row - OUTER_RADIUS_PIXELS)
    r1 = min(height, row + OUTER_RADIUS_PIXELS + 1)
    c0 = max(0, column - OUTER_RADIUS_PIXELS)
    c1 = min(width, column + OUTER_RADIUS_PIXELS + 1)

    ring = np.ones((r1 - r0, c1 - c0), dtype=bool)
    inner_r0 = max(row - EXCLUDED_CENTER_RADIUS_PIXELS, r0) - r0
    inner_r1 = min(row + EXCLUDED_CENTER_RADIUS_PIXELS + 1, r1) - r0
    inner_c0 = max(column - EXCLUDED_CENTER_RADIUS_PIXELS, c0) - c0
    inner_c1 = min(column + EXCLUDED_CENTER_RADIUS_PIXELS + 1, c1) - c0
    ring[inner_r0:inner_r1, inner_c0:inner_c1] = False

    expected_ring = (2 * OUTER_RADIUS_PIXELS + 1) ** 2 - (
        2 * EXCLUDED_CENTER_RADIUS_PIXELS + 1
    ) ** 2
    present_ring = int(ring.sum())
    geometry_fraction = present_ring / expected_ring if expected_ring else 0.0

    local_valid = valid_mask[r0:r1, c0:c1] & ring
    valid_count = int(local_valid.sum())
    valid_fraction = valid_count / present_ring if present_ring else 0.0
    if valid_count <= 0:
        return None, None, valid_fraction, geometry_fraction

    previous_patch = previous_delta[r0:r1, c0:c1]
    current_patch = current_delta[r0:r1, c0:c1]
    return (
        float(np.mean(previous_patch[local_valid])),
        float(np.mean(current_patch[local_valid])),
        valid_fraction,
        geometry_fraction,
    )


def _flags(inner_current, ring_current, valid_fraction, geometry_fraction):
    if inner_current is None or ring_current is None:
        return None, False, False, "YETERSIZ_VERI"

    inner = abs(float(inner_current))
    ring = abs(float(ring_current))
    contrast = inner / max(ring, 0.01)
    enough = valid_fraction >= MIN_RING_VALID_FRACTION and geometry_fraction >= 0.80
    widespread = bool(
        enough
        and ring >= max(WIDESPREAD_RING_ABS_MIN, inner * WIDESPREAD_RING_RELATIVE_MIN)
    )
    local_support = bool(
        enough
        and inner >= MIN_INNER_BSI_CHANGE
        and contrast >= LOCAL_CONTRAST_MIN
        and not widespread
    )
    if not enough:
        status = "YETERSIZ_CEVRE_ORNEGI"
    elif local_support:
        status = "LOKAL_DESTEK"
    elif widespread:
        status = "YAYGIN_CEVRE_RISKI"
    else:
        status = "BELIRSIZ"
    return round(contrast, 2), local_support, widespread, status


def _candidate_rows(watch, region_key):
    label = str(satellite.REGIONS[region_key]["label"])
    rows = []
    for raw in (watch or {}).get("adaylar") or []:
        if not isinstance(raw, dict) or str(raw.get("bolge") or "") != label:
            continue
        area = _number(raw.get("alan_m2"))
        if LOWER_EXCLUSIVE_M2 < area <= UPPER_INCLUSIVE_M2:
            rows.append(raw)
    return rows


def _analyze_region(region_key, temporal_region, watch):
    candidates = _candidate_rows(watch, region_key)
    label = satellite.REGIONS[region_key]["label"]
    if not candidates:
        return {
            "durum": "ok",
            "bolge": label,
            "aday": 0,
            "lokal_destek": 0,
            "yaygin_cevre_riski": 0,
            "gulbahce_2km_aday": 0,
            "adaylar": [],
        }
    if not isinstance(temporal_region, dict) or temporal_region.get("durum") != "ok":
        return {
            "durum": "atlandi",
            "bolge": label,
            "neden": "temporal_bolge_hazir_degil",
            "aday": len(candidates),
            "adaylar": [],
        }

    bbox = satellite.REGIONS[region_key]["bbox"]
    items = satellite._search_items(bbox)
    previous = temporal._find_item(items, temporal_region.get("degisim_oncesi_item"))
    older = temporal._find_item(items, temporal_region.get("onceki_item"))
    latest = temporal._find_item(items, temporal_region.get("son_item"))
    if previous is None or older is None or latest is None:
        return {
            "durum": "atlandi",
            "bolge": label,
            "neden": "temporal_sahnelerden_biri_bulunamadi",
            "aday": len(candidates),
            "adaylar": [],
        }

    height, width = satellite._output_shape(bbox)
    previous_bsi, previous_scl = temporal._bsi_for_item(previous, bbox, height, width)
    older_bsi, older_scl = temporal._bsi_for_item(older, bbox, height, width)
    latest_bsi, latest_scl = temporal._bsi_for_item(latest, bbox, height, width)
    previous_delta = np.abs(older_bsi - previous_bsi)
    current_delta = np.abs(latest_bsi - older_bsi)

    valid = ~np.isin(previous_scl, satellite.EXCLUDED_SCL_CLASSES)
    valid &= ~np.isin(older_scl, satellite.EXCLUDED_SCL_CLASSES)
    valid &= ~np.isin(latest_scl, satellite.EXCLUDED_SCL_CLASSES)

    rows = []
    for raw in candidates:
        lat = _number(raw.get("enlem"))
        lon = _number(raw.get("boylam"))
        row, column = temporal._pixel_for_point(lat, lon, bbox, current_delta.shape)
        row_slice, col_slice = temporal._patch_slices(row, column, current_delta.shape)
        _, inner_current, inner_valid = temporal._paired_patch_means(
            previous_delta, current_delta, valid, row_slice, col_slice
        )
        ring_previous, ring_current, ring_valid, ring_geometry = _upper_ring_means(
            previous_delta, current_delta, valid, row, column
        )
        contrast, local_support, widespread, status = _flags(
            inner_current, ring_current, ring_valid, ring_geometry
        )
        gulbahce_distance = _distance_m(lat, lon, GULBAHCE_LAT, GULBAHCE_LON)
        rows.append(
            {
                "mahalle": str(raw.get("mahalle") or "Mevki doğrulanmadı"),
                "enlem": round(lat, 6),
                "boylam": round(lon, 6),
                "alan_m2": round(_number(raw.get("alan_m2"))),
                "son_item": latest.get("id"),
                "son_tarih": temporal_region.get("son_tarih"),
                "ic_3x3_son_bsi_degisim": (
                    round(inner_current, 4) if inner_current is not None else None
                ),
                "ic_3x3_gecerli_oran": round(inner_valid, 3),
                "cevre_7x7_dis_halka_onceki_bsi_degisim": (
                    round(ring_previous, 4) if ring_previous is not None else None
                ),
                "cevre_7x7_dis_halka_son_bsi_degisim": (
                    round(ring_current, 4) if ring_current is not None else None
                ),
                "cevre_7x7_dis_halka_gecerli_oran": round(ring_valid, 3),
                "cevre_7x7_dis_halka_geometri_orani": round(ring_geometry, 3),
                "yerellik_orani": contrast,
                "ust_bant_lokal_destek": bool(local_support),
                "yaygin_cevre_degisim_riski": bool(widespread),
                "yerellik_durumu": status,
                "ana_goreve_40m_yakin": bool(raw.get("ana_goreve_40m_yakin")),
                "gulbahce_merkeze_mesafe_m": round(gulbahce_distance, 1),
                "gulbahce_2km_operasyon_penceresinde": bool(
                    gulbahce_distance <= GULBAHCE_RADIUS_M
                ),
                "alarm": False,
                "saha_gorevi": False,
                "harita": raw.get("harita"),
            }
        )

    rows.sort(
        key=lambda item: (
            not bool(item.get("ust_bant_lokal_destek")),
            bool(item.get("yaygin_cevre_degisim_riski")),
            -_number(item.get("yerellik_orani")),
            -_number(item.get("ic_3x3_son_bsi_degisim")),
        )
    )
    return {
        "durum": "ok",
        "bolge": label,
        "degisim_oncesi_item": previous.get("id"),
        "onceki_item": older.get("id"),
        "son_item": latest.get("id"),
        "aday": len(rows),
        "lokal_destek": sum(1 for row in rows if row.get("ust_bant_lokal_destek")),
        "yaygin_cevre_riski": sum(
            1 for row in rows if row.get("yaygin_cevre_degisim_riski")
        ),
        "gulbahce_2km_aday": sum(
            1 for row in rows if row.get("gulbahce_2km_operasyon_penceresinde")
        ),
        "adaylar": rows,
    }


def build_audit(watch, temporal_payload):
    if not isinstance(watch, dict) or not isinstance(temporal_payload, dict):
        return None
    report_date = str(watch.get("rapor_tarihi") or "")
    if not report_date or str(temporal_payload.get("rapor_tarihi") or "") != report_date:
        return None

    payload = {
        "rapor_tarihi": report_date,
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "amac": (
            "901-2.000 m² güçlü kuru-zemin temporal adayında 3x3 merkez değişimini "
            "7x7 pencerenin merkez 5x5 dışındaki halkasına karşı ölçmek."
        ),
        "ana_uretim_esigi_m2": 250,
        "mikro_santiye_bandi_m2": [150, 249],
        "izlenen_ust_bant_m2": [901, 2000],
        "alarm": False,
        "saha_gorevi": False,
        "operasyonel": False,
        "gulbahce_operasyon_penceresi": {
            "merkez": [GULBAHCE_LAT, GULBAHCE_LON],
            "yaricap_m": GULBAHCE_RADIUS_M,
            "not": "Yalnız kör-alan diagnostik referansıdır; idari/kadastral sınır değildir.",
        },
        "esikler": {
            "ic_yama": "3x3",
            "cevre_penceresi": "7x7_merkez_5x5_haric_halka",
            "minimum_halka_gecerli_oran": MIN_RING_VALID_FRACTION,
            "lokal_kontrast_min": LOCAL_CONTRAST_MIN,
            "minimum_ic_bsi_degisim": MIN_INNER_BSI_CHANGE,
            "yaygin_halka_mutlak_bsi_min": WIDESPREAD_RING_ABS_MIN,
            "yaygin_halka_goreli_min": WIDESPREAD_RING_RELATIVE_MIN,
        },
        "uyari": (
            "Diagnostiktir; lokal destek tek başına şantiye değildir. 250 m² ana eşik "
            "ve 150-249 m² MİKRO ŞANTİYE alarm/görev politikası değiştirilmez."
        ),
        "bolgeler": {},
    }

    temporal_regions = temporal_payload.get("bolgeler") or {}
    for region_key in ("cesme", "uzunkuyu"):
        try:
            payload["bolgeler"][region_key] = _analyze_region(
                region_key, temporal_regions.get(region_key) or {}, watch
            )
        except Exception as exc:
            payload["bolgeler"][region_key] = {
                "durum": "hata",
                "bolge": satellite.REGIONS[region_key]["label"],
                "neden": str(exc),
                "aday": len(_candidate_rows(watch, region_key)),
                "adaylar": [],
            }

    all_rows = []
    for region in payload["bolgeler"].values():
        all_rows.extend(region.get("adaylar") or [])
    payload["ust_bant_aday_toplam"] = len(all_rows)
    payload["ust_bant_lokal_destek_toplam"] = sum(
        1 for row in all_rows if row.get("ust_bant_lokal_destek")
    )
    payload["yaygin_cevre_riski_toplam"] = sum(
        1 for row in all_rows if row.get("yaygin_cevre_degisim_riski")
    )
    payload["gulbahce_2km_ust_bant_aday"] = sum(
        1 for row in all_rows if row.get("gulbahce_2km_operasyon_penceresinde")
    )
    payload["adaylar"] = sorted(
        all_rows,
        key=lambda item: (
            not bool(item.get("ust_bant_lokal_destek")),
            bool(item.get("yaygin_cevre_degisim_riski")),
            -_number(item.get("yerellik_orani")),
        ),
    )
    return payload


def _stable(payload):
    value = dict(payload)
    value.pop("olusturma", None)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def write_audit():
    payload = build_audit(_load(WATCH_JSON), _load(TEMPORAL_JSON))
    if payload is None:
        raise RuntimeError("Üst bant watch ve temporal audit aynı güne ait değil.")
    before = _load(OUTPUT_JSON)
    changed = before is None or _stable(before) != _stable(payload)
    if changed:
        OUTPUT_JSON.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return changed, payload


def _self_check():
    previous = np.zeros((7, 7), dtype="float32") + 0.01
    current = np.zeros((7, 7), dtype="float32") + 0.02
    current[2:5, 2:5] = 0.20
    valid = np.ones((7, 7), dtype=bool)
    prev_ring, cur_ring, valid_fraction, geometry_fraction = _upper_ring_means(
        previous, current, valid, 3, 3
    )
    assert abs(float(prev_ring) - 0.01) < 1e-6
    assert abs(float(cur_ring) - 0.02) < 1e-6
    assert valid_fraction == 1.0 and geometry_fraction == 1.0
    contrast, local, widespread, status = _flags(
        0.20, cur_ring, valid_fraction, geometry_fraction
    )
    assert contrast >= 9.0 and local and not widespread
    assert status == "LOKAL_DESTEK"

    broad = np.zeros((7, 7), dtype="float32") + 0.15
    _, broad_ring, valid_fraction, geometry_fraction = _upper_ring_means(
        previous, broad, valid, 3, 3
    )
    _, local, widespread, status = _flags(
        0.16, broad_ring, valid_fraction, geometry_fraction
    )
    assert not local and widespread and status == "YAYGIN_CEVRE_RISKI"
    print("dry_ground_upper_band_locality self-check: OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        return 0
    changed, payload = write_audit()
    print(
        "Üst bant yerellik: "
        f"aday={payload['ust_bant_aday_toplam']}, "
        f"lokal={payload['ust_bant_lokal_destek_toplam']}, "
        f"yaygın={payload['yaygin_cevre_riski_toplam']}, "
        f"Gülbahçe2km={payload['gulbahce_2km_ust_bant_aday']}, "
        f"dosya_değişti={changed}. Alarm/görev üretilmedi."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
