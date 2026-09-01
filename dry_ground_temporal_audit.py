"""Kuru-zemin kalibrasyon adaylarında değişimin gerçekten yeni başlayıp başlamadığını ölçer.

`dry_ground_gap_audit.py` yalnız son Sentinel çiftindeki, üretim alarm maskesi dışında kalan
250-2.000 m² kuru-zemin değişimlerini diagnostik olarak çıkarır. Yaz sonunda tarla/toprak
yüzeyleri de güçlü BSI farkı üretebildiği için bu ek denetim, yalnız mevcut saha-benzeri
örneklerde bir önceki uygun Sentinel sahnesini kullanarak değişim öncesi zeminin kararlı
olup olmadığını ölçer.

Bu dosya alarm, görev veya üretim eşiği değiştirmez. Çıktısı yalnız saha kalibrasyonu ve
yanlış-pozitif azaltma kararları içindir.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np

import satellite
from daily_report import ISTANBUL


SOURCE_AUDIT = Path(__file__).with_name("dry_ground_gap_audit.json")
OUTPUT_AUDIT = Path(__file__).with_name("dry_ground_temporal_audit.json")
PATCH_RADIUS_PIXELS = 1
# 3x3 yamada 6/9 geçerli pikseli tam olarak kabul et; 0.67 yazmak 6/9=0.666...
# örneklerini yanlışlıkla sınırın altında bırakıyordu.
MIN_VALID_FRACTION = 2 / 3
PRECHANGE_ABS_BSI_CAP = 0.08
PRECHANGE_RELATIVE_MAX = 0.50
ABRUPT_RATIO_MIN = 2.0
UNSTABLE_ABS_BSI_MIN = 0.10
UNSTABLE_RELATIVE_MIN = 0.80
MIN_PREVIOUS_GAP_DAYS = 2
REQUIRED_ASSETS = ("blue", "red", "nir", "swir16", "scl")


def _item_time(item):
    raw = str(item.get("properties", {}).get("datetime") or "")
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _usable(item, bbox):
    assets = item.get("assets", {})
    return satellite._item_covers_bbox(item, bbox) and all(
        key in assets for key in REQUIRED_ASSETS
    )


def _find_item(items, item_id):
    wanted = str(item_id or "")
    return next((item for item in items if str(item.get("id") or "") == wanted), None)


def _previous_scene(items, older, bbox):
    """Eski sahneden önce karşılaştırılabilir en güvenli sahneyi seç."""
    older_time = _item_time(older)
    if older_time is None:
        return None

    candidates = []
    for item in items:
        if item is older or not _usable(item, bbox):
            continue
        item_time = _item_time(item)
        if item_time is None:
            continue
        gap_days = (older_time - item_time).total_seconds() / 86400
        if gap_days < MIN_PREVIOUS_GAP_DAYS:
            continue
        if not satellite._same_mgrs_tile(item, older):
            continue
        candidates.append(item)

    if not candidates:
        return None

    # Aynı göreli yörünge bulunabiliyorsa geometri/paralaks farkını azaltmak için
    # onu tercih et. Yoksa en yakın tam-kapsam aynı-MGRS sahnesine geri düş.
    older_orbit = satellite._relative_orbit(older)
    if older_orbit is not None:
        same_orbit = [
            item for item in candidates
            if satellite._relative_orbit(item) == older_orbit
        ]
        if same_orbit:
            candidates = same_orbit

    return max(candidates, key=lambda item: _item_time(item))


def _bsi_for_item(item, bbox, height, width):
    blue = satellite._reflectance(
        satellite._read_asset(item, "blue", bbox, height, width, "bilinear")[0]
    )
    red = satellite._reflectance(
        satellite._read_asset(item, "red", bbox, height, width, "bilinear")[0]
    )
    nir = satellite._reflectance(
        satellite._read_asset(item, "nir", bbox, height, width, "bilinear")[0]
    )
    swir = satellite._reflectance(
        satellite._read_asset(item, "swir16", bbox, height, width, "bilinear")[0]
    )
    scl = satellite._read_asset(
        item, "scl", bbox, height, width, "nearest"
    )[0]
    return _bsi(blue, red, nir, swir), scl


def _bsi(blue, red, nir, swir):
    numerator = (swir + red) - (nir + blue)
    denominator = (swir + red) + (nir + blue)
    return np.divide(
        numerator,
        denominator,
        out=np.zeros_like(red, dtype="float32"),
        where=np.abs(denominator) > 0.001,
    )


def _pixel_for_point(latitude, longitude, bbox, shape):
    height, width = shape
    west, south, east, north = bbox
    row = int((north - float(latitude)) / (north - south) * height)
    column = int((float(longitude) - west) / (east - west) * width)
    row = min(max(row, 0), height - 1)
    column = min(max(column, 0), width - 1)
    return row, column


def _patch_slices(row, column, shape):
    height, width = shape
    radius = PATCH_RADIUS_PIXELS
    return (
        slice(max(0, row - radius), min(height, row + radius + 1)),
        slice(max(0, column - radius), min(width, column + radius + 1)),
    )


def _paired_patch_means(previous_delta, current_delta, valid_mask, row_slice, col_slice):
    """İki Sentinel dönemini aynı piksel yaması ve aynı geçerli piksellerle ölç."""
    patch_valid = valid_mask[row_slice, col_slice]
    total = int(patch_valid.size)
    valid_count = int(patch_valid.sum())
    valid_fraction = valid_count / max(total, 1)
    if not valid_count:
        return None, None, valid_fraction

    previous_patch = previous_delta[row_slice, col_slice]
    current_patch = current_delta[row_slice, col_slice]
    return (
        float(np.mean(previous_patch[patch_valid])),
        float(np.mean(current_patch[patch_valid])),
        valid_fraction,
    )


def _classify(current_bsi_delta, previous_bsi_delta, valid_fraction):
    current = abs(float(current_bsi_delta or 0))
    previous = abs(float(previous_bsi_delta or 0))
    valid = float(valid_fraction or 0)
    ratio = current / max(previous, 0.01)

    abrupt = bool(
        valid >= MIN_VALID_FRACTION
        and current >= 0.10
        and previous <= min(PRECHANGE_ABS_BSI_CAP, current * PRECHANGE_RELATIVE_MAX)
        and ratio >= ABRUPT_RATIO_MIN
    )
    unstable = bool(
        valid >= MIN_VALID_FRACTION
        and previous >= max(UNSTABLE_ABS_BSI_MIN, current * UNSTABLE_RELATIVE_MIN)
    )
    return round(ratio, 2), abrupt, unstable


def _unique_candidates(region_data):
    rows = []
    seen = set()
    for raw in region_data.get("saha_benzeri_ornekler") or []:
        if not isinstance(raw, dict):
            continue
        try:
            point = (round(float(raw["enlem"]), 5), round(float(raw["boylam"]), 5))
        except (KeyError, TypeError, ValueError):
            continue
        if point in seen:
            continue
        seen.add(point)
        rows.append(dict(raw))
    return rows


def _analyze_region(region_key, region_data):
    bbox = satellite.REGIONS[region_key]["bbox"]
    items = satellite._search_items(bbox)
    older = _find_item(items, region_data.get("onceki_item"))
    latest = _find_item(items, region_data.get("son_item"))
    if older is None:
        return {
            "durum": "atlandi",
            "neden": "onceki_sentinel_sahnesi_bulunamadi",
        }
    if latest is None:
        return {
            "durum": "atlandi",
            "neden": "son_sentinel_sahnesi_bulunamadi",
            "onceki_item": older.get("id"),
        }

    previous = _previous_scene(items, older, bbox)
    if previous is None:
        return {
            "durum": "atlandi",
            "neden": "degisim_oncesi_uygun_sentinel_sahnesi_bulunamadi",
            "onceki_item": older.get("id"),
        }

    height, width = satellite._output_shape(bbox)
    previous_bsi, previous_scl = _bsi_for_item(previous, bbox, height, width)
    older_bsi, older_scl = _bsi_for_item(older, bbox, height, width)
    latest_bsi, latest_scl = _bsi_for_item(latest, bbox, height, width)
    previous_delta = np.abs(older_bsi - previous_bsi)
    current_delta = np.abs(latest_bsi - older_bsi)

    # İki dönemin ortalaması aynı 3x3 Sentinel yamasından ve üç sahnenin tamamında
    # geçerli olan aynı piksellerden alınır. Böylece son çiftte bileşen ortalaması,
    # önceki dönemde çevre yaması ortalaması kıyaslanarak sahte "ani başlangıç"
    # üretilmez.
    temporal_valid = ~np.isin(previous_scl, satellite.EXCLUDED_SCL_CLASSES)
    temporal_valid &= ~np.isin(older_scl, satellite.EXCLUDED_SCL_CLASSES)
    temporal_valid &= ~np.isin(latest_scl, satellite.EXCLUDED_SCL_CLASSES)

    rows = []
    for raw in _unique_candidates(region_data):
        row, column = _pixel_for_point(
            raw.get("enlem"), raw.get("boylam"), bbox, previous_delta.shape
        )
        row_slice, col_slice = _patch_slices(
            row, column, previous_delta.shape
        )
        previous_mean, current_mean, valid_fraction = _paired_patch_means(
            previous_delta,
            current_delta,
            temporal_valid,
            row_slice,
            col_slice,
        )
        ratio, abrupt, unstable = _classify(
            current_mean,
            previous_mean,
            valid_fraction,
        )
        source_component_mean = float(raw.get("ortalama_bsi_degisim") or 0)
        item = {
            "mahalle": raw.get("mahalle"),
            "enlem": raw.get("enlem"),
            "boylam": raw.get("boylam"),
            "alan_m2": raw.get("alan_m2"),
            "son_cift_bsi_degisim": (
                round(current_mean, 4) if current_mean is not None else None
            ),
            "kaynak_bilesen_bsi_degisim": round(source_component_mean, 4),
            "onceki_donem_bsi_degisim": (
                round(previous_mean, 4) if previous_mean is not None else None
            ),
            "uc_sahne_gecerli_oran": round(valid_fraction, 3),
            # Eski tüketiciler için alanı koru; artık üç sahnenin ortak geçerli oranıdır.
            "onceki_donem_gecerli_oran": round(valid_fraction, 3),
            "ani_baslangic_orani": ratio,
            "ani_baslangic_destegi": abrupt,
            "istikrarsiz_zemin_riski": unstable,
            "izole_saha_benzeri": bool(raw.get("izole_saha_benzeri")),
            "lineer_geometri_riski": bool(raw.get("lineer_geometri_riski")),
        }
        rows.append(item)

    rows.sort(
        key=lambda item: (
            not bool(item.get("ani_baslangic_destegi")),
            bool(item.get("istikrarsiz_zemin_riski")),
            -float(item.get("ani_baslangic_orani") or 0),
            -float(item.get("son_cift_bsi_degisim") or 0),
        )
    )
    previous_time = _item_time(previous)
    older_time = _item_time(older)
    gap_days = (
        round((older_time - previous_time).total_seconds() / 86400, 1)
        if previous_time and older_time
        else None
    )

    return {
        "durum": "ok",
        "bolge": satellite.REGIONS[region_key]["label"],
        "degisim_oncesi_item": previous.get("id"),
        "degisim_oncesi_tarih": satellite._item_date(previous),
        "onceki_item": older.get("id"),
        "onceki_tarih": satellite._item_date(older),
        "son_item": latest.get("id"),
        "son_tarih": satellite._item_date(latest),
        "temporal_ornekleme": "ayni_3x3_yama_uc_sahne_ortak_gecerli_piksel",
        "degisim_oncesi_aralik_gun": gap_days,
        "olculen_aday": len(rows),
        "ani_baslangic_destegi": sum(
            1 for item in rows if item.get("ani_baslangic_destegi")
        ),
        "istikrarsiz_zemin_riski": sum(
            1 for item in rows if item.get("istikrarsiz_zemin_riski")
        ),
        "adaylar": rows,
    }


def _self_check():
    ratio, abrupt, unstable = _classify(0.24, 0.05, 1.0)
    assert ratio >= 4.0
    assert abrupt
    assert not unstable

    ratio, abrupt, unstable = _classify(0.16, 0.14, 1.0)
    assert not abrupt
    assert unstable

    # 3x3 yamanın tam 6/9 geçerli pikseli sınırı geçmelidir.
    ratio, abrupt, unstable = _classify(0.24, 0.04, 6 / 9)
    assert abrupt
    assert not unstable

    ratio, abrupt, unstable = _classify(0.24, 0.04, 5 / 9)
    assert not abrupt
    assert not unstable

    # Önceki ve son dönem kesinlikle aynı ortak geçerli piksellerden ölçülmelidir.
    previous = np.arange(9, dtype="float32").reshape(3, 3) / 100
    current = previous + 0.20
    valid = np.ones((3, 3), dtype=bool)
    valid[0, :3] = False
    previous_mean, current_mean, valid_fraction = _paired_patch_means(
        previous,
        current,
        valid,
        slice(0, 3),
        slice(0, 3),
    )
    assert valid_fraction == 6 / 9
    assert previous_mean is not None and current_mean is not None
    assert abs((current_mean - previous_mean) - 0.20) < 1e-6


def run_audit():
    _self_check()
    if not SOURCE_AUDIT.exists():
        raise RuntimeError("dry_ground_gap_audit.json bulunamadı.")

    source = json.loads(SOURCE_AUDIT.read_text(encoding="utf-8"))
    payload = {
        "rapor_tarihi": source.get("rapor_tarihi"),
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "amac": (
            "Kuru-zemin saha-benzeri kalibrasyon örneklerinde son değişimden önceki "
            "Sentinel döneminin BSI kararlılığını aynı 3x3 piksel yamasında ölçmek; "
            "ani başlangıç desteği ve istikrarsız zemin riskini yalnız diagnostik "
            "olarak işaretlemek."
        ),
        "esikler": {
            "patch_yaricap_piksel": PATCH_RADIUS_PIXELS,
            "minimum_gecerli_oran": MIN_VALID_FRACTION,
            "ani_baslangic_onceki_mutlak_bsi_tavani": PRECHANGE_ABS_BSI_CAP,
            "ani_baslangic_onceki_goreli_bsi_tavani": PRECHANGE_RELATIVE_MAX,
            "ani_baslangic_min_oran": ABRUPT_RATIO_MIN,
            "istikrarsiz_mutlak_bsi_min": UNSTABLE_ABS_BSI_MIN,
            "istikrarsiz_goreli_bsi_min": UNSTABLE_RELATIVE_MIN,
            "minimum_onceki_sahne_araligi_gun": MIN_PREVIOUS_GAP_DAYS,
        },
        "uyari": (
            "Bu çıktı alarm veya görev değildir; üretim maskesi/eşiği değişmez. "
            "Saha doğrulaması olmadan ani başlangıç etiketi yeni şantiye kabul edilmez."
        ),
        "bolgeler": {},
    }

    for region_key, region_data in (source.get("bolgeler") or {}).items():
        if region_key not in satellite.REGIONS:
            continue
        if not isinstance(region_data, dict) or region_data.get("durum") != "ok":
            payload["bolgeler"][region_key] = {
                "durum": "atlandi",
                "neden": "kuru_zemin_deneti_ok_degil",
            }
            continue
        try:
            payload["bolgeler"][region_key] = _analyze_region(
                region_key, region_data
            )
        except Exception as exc:
            payload["bolgeler"][region_key] = {
                "durum": "hata",
                "neden": str(exc),
            }

    OUTPUT_AUDIT.write_text(
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
            "Kuru zemin zaman-serisi öz testi başarılı; üretim alarmı değişmedi."
        )
        return

    payload = run_audit()
    parts = []
    for region_key, data in payload.get("bolgeler", {}).items():
        if data.get("durum") == "ok":
            parts.append(
                f"{region_key}={int(data.get('olculen_aday') or 0)} "
                f"(ani={int(data.get('ani_baslangic_destegi') or 0)}, "
                f"istikrarsiz={int(data.get('istikrarsiz_zemin_riski') or 0)})"
            )
        else:
            parts.append(f"{region_key}={data.get('durum')}")
    print(
        "Kuru zemin zaman-serisi denetimi tamamlandı: "
        + (", ".join(parts) or "bölge yok")
        + ". Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
