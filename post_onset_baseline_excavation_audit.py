"""13 Eylül sakin baseline ile en güncel 17 Eylül sonrası Sentinel-2 sahnesini karşılaştırır.

Bu katman yalnız diagnostiktir. Alarm, saha görevi veya rota üretmez; 250 m² ana eşik
ve 150–249 m² MİKRO politikası değişmez. Amaç 15–17 Eylül başlangıç penceresinde
saha-doğrulanmış gerçek kazının uzun-baseline imzasını dört kritik yanlış pozitifle
aynı geometri altında karşılaştırmak ve benzer lokal adayları kaybetmeden ölçmektir.

Yalnız tam AOI kapsayan, aynı MGRS ve aynı göreli yörüngedeki Sentinel-2 çifti kullanılır.
En güncel uygun post-onset sahne kritik regresyon referansında yeterli geçerli piksel
taşımıyorsa görüntü varmış gibi kabul edilmez; bir önceki gerçekten kullanılabilir aynı-
geometri sahneye güvenli biçimde geri düşülür. Sentinel-2 10 m veriden gerçek kazı
derinliği ölçüldüğü iddia edilmez.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path

import numpy as np

import localized_excavation_seed_audit as seed_audit
import excavation_reference_signature_audit as signature
import seed_centered_excavation_morphology_audit as morphology
import satellite


OUTPUT = Path(__file__).with_name("post_onset_baseline_excavation_review.json")
BASELINE_DATE = "2026-09-13"
POST_ONSET_AFTER = "2026-09-17"
REGION_KEYS = ("cesme", "uzunkuyu")
SIMILAR_LIMIT = 20
FP_SUPPRESS_RADIUS_M = 45.0
MIN_REFERENCE_VALID_PIXELS = 9
MAX_POST_SCENE_ATTEMPTS = 6

CRITICAL_POINTS = (
    {
        "id": "GERCEK_KAZI",
        "sonuc": "DOGRULANMIS_KAZI",
        "enlem": 38.341846,
        "boylam": 26.432861,
    },
    {
        "id": "FP_REISDERE",
        "sonuc": "YANLIS_POZITIF",
        "enlem": 38.322140,
        "boylam": 26.407912,
    },
    {
        "id": "FP_UZUNKUYU",
        "sonuc": "YANLIS_POZITIF",
        "enlem": 38.320150,
        "boylam": 26.562138,
    },
    {
        "id": "FP_MUSALLA_1",
        "sonuc": "YANLIS_POZITIF",
        "enlem": 38.313186,
        "boylam": 26.307059,
    },
    {
        "id": "FP_MUSALLA_2",
        "sonuc": "YANLIS_POZITIF",
        "enlem": 38.308120,
        "boylam": 26.326863,
    },
)


def _date(item):
    raw = str((item.get("properties") or {}).get("datetime") or "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _cloud(item):
    try:
        return float((item.get("properties") or {}).get("eo:cloud_cover"))
    except (TypeError, ValueError):
        return 999.0


def _full(item, bbox):
    assets = item.get("assets") or {}
    return satellite._item_covers_bbox(item, bbox) and all(
        key in assets for key in ("visual", "red", "nir", "scl")
    )


def _candidate_pairs(region_key):
    """En güncelden eskiye aynı-geometri baseline→post-onset çiftlerini döndürür."""
    bbox = satellite.REGIONS[region_key]["bbox"]
    items = satellite._search_items(bbox, days=30, max_cloud=100)
    baseline_day = datetime.fromisoformat(BASELINE_DATE).date()
    post_day = datetime.fromisoformat(POST_ONSET_AFTER).date()

    baseline = [
        item for item in items
        if _full(item, bbox) and _date(item) == baseline_day
    ]
    posts = [
        item for item in items
        if _full(item, bbox) and _date(item) is not None and _date(item) > post_day
    ]
    baseline.sort(key=_cloud)
    posts.sort(key=lambda item: (_date(item), -_cloud(item)), reverse=True)

    if not baseline:
        return [], "BASELINE_VERI_YOK"
    if not posts:
        return [], "POST_ONSET_VERI_YOK"

    pairs = []
    seen_latest = set()
    post_dates = sorted({_date(item) for item in posts}, reverse=True)
    for target_day in post_dates:
        day_posts = sorted(
            [item for item in posts if _date(item) == target_day],
            key=_cloud,
        )
        for latest in day_posts:
            latest_id = str(latest.get("id") or "")
            if latest_id and latest_id in seen_latest:
                continue
            for older in baseline:
                if not satellite._same_mgrs_tile(older, latest):
                    continue
                old_orbit = satellite._relative_orbit(older)
                new_orbit = satellite._relative_orbit(latest)
                if old_orbit is None or new_orbit is None or old_orbit != new_orbit:
                    continue
                pairs.append((older, latest))
                if latest_id:
                    seen_latest.add(latest_id)
                break
            if len(pairs) >= MAX_POST_SCENE_ATTEMPTS:
                return pairs, "AYNI_MGRS_AYNI_YORUNGE"

    if pairs:
        return pairs, "AYNI_MGRS_AYNI_YORUNGE"
    return [], "GEOMETRI_UYUMSUZ"


def _choose_pair(region_key):
    """Geriye dönük yardımcı: yalnız ilk aynı-geometri çifti döndürür."""
    pairs, status = _candidate_pairs(region_key)
    return (pairs[0] if pairs else None), status


def _arrays_for_pair(region_key, pair):
    original = satellite.sentinel_pair
    try:
        satellite.sentinel_pair = lambda key: pair if key == region_key else original(key)
        return signature._region_arrays(region_key)
    finally:
        satellite.sentinel_pair = original


def _distance_m(a, b):
    lat1, lon1 = float(a[0]), float(a[1])
    lat2, lon2 = float(b[0]), float(b[1])
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _in_bbox(point, bbox):
    west, south, east, north = bbox
    return south <= point["enlem"] <= north and west <= point["boylam"] <= east


def _reference_visibility(arrays):
    """Kalibrasyon referansının gerçekten gözlenebilir olup olmadığını ölçer.

    Çeşme kutusunda doğrulanmış kazı referansı varsa o noktanın 5x5 çevresinde en az
    3x3 eşdeğeri geçerli piksel isteriz. Pozitif referans bulunmayan bölgede en az bir
    kritik FP referansının aynı desteği taşıması yeterlidir. Bu kontrol sinyal var/yok
    kararı değildir; yalnız bulut/no-data nedeniyle tamamı maskelenmiş bir sahnenin
    'en güncel kullanılabilir' diye seçilmesini engeller.
    """
    rows = []
    for point in CRITICAL_POINTS:
        if not _in_bbox(point, arrays["bbox"]):
            continue
        row, col = signature._row_col(
            point["enlem"], point["boylam"], arrays["bbox"], arrays["shape"]
        )
        valid5 = signature._window(arrays["valid"], row, col, 2)
        valid_count = int(np.count_nonzero(valid5))
        rows.append({
            "id": point["id"],
            "sonuc": point["sonuc"],
            "gecerli_piksel_5x5": valid_count,
            "yeterli": valid_count >= MIN_REFERENCE_VALID_PIXELS,
        })

    positives = [row for row in rows if row["sonuc"] == "DOGRULANMIS_KAZI"]
    if positives:
        usable = all(row["yeterli"] for row in positives)
        reason = "DOGRULANMIS_KAZI_REFERANSI_GORUNUR" if usable else "DOGRULANMIS_KAZI_REFERANSI_MASKELI"
    else:
        usable = any(row["yeterli"] for row in rows)
        reason = "KRITIK_REFERANS_GORUNUR" if usable else "KRITIK_REFERANSLAR_MASKELI"

    return {
        "kullanilabilir": bool(usable),
        "neden": reason,
        "min_gecerli_piksel_5x5": MIN_REFERENCE_VALID_PIXELS,
        "referanslar": rows,
    }


def _point_eval(point, arrays):
    row, col = signature._row_col(
        point["enlem"], point["boylam"], arrays["bbox"], arrays["shape"]
    )
    metrics = seed_audit._window_metrics(arrays, row, col)
    seed_pass = seed_audit._passes(metrics)
    seed_class = seed_audit._pass_class(metrics)
    morph = morphology._score_at(arrays, point["enlem"], point["boylam"])
    detected = bool(
        seed_pass
        and str(morph.get("seed_merkezli_morfoloji_seviyesi") or "").upper()
        in {"ORTA", "YUKSEK"}
    )
    expected = point["sonuc"] == "DOGRULANMIS_KAZI"
    return {
        **point,
        "lokal_seed_uyumu": seed_pass,
        "seed_sinifi": seed_class,
        "seed_merkezli_morfoloji_puani": morph.get("seed_merkezli_morfoloji_puani"),
        "seed_merkezli_morfoloji_seviyesi": morph.get("seed_merkezli_morfoloji_seviyesi"),
        "baseline_post_proxy_tespit": detected,
        "regresyon_uyumlu": detected == expected,
        **metrics,
    }


def _similar_candidates(region_key, arrays, true_class):
    if not true_class:
        return []
    discovered = seed_audit._discover(region_key, arrays)
    fps = [
        (point["enlem"], point["boylam"])
        for point in CRITICAL_POINTS
        if point["sonuc"] == "YANLIS_POZITIF"
    ]
    true_point = next(
        (point for point in CRITICAL_POINTS if point["sonuc"] == "DOGRULANMIS_KAZI"),
        None,
    )
    rows = []
    for candidate in discovered:
        if candidate.get("seed_sinifi") != true_class:
            continue
        candidate_point = (candidate["enlem"], candidate["boylam"])
        if any(_distance_m(candidate_point, fp) <= FP_SUPPRESS_RADIUS_M for fp in fps):
            continue
        if true_point and _distance_m(
            candidate_point, (true_point["enlem"], true_point["boylam"])
        ) <= FP_SUPPRESS_RADIUS_M:
            continue
        morph = morphology._score_at(
            arrays, candidate["enlem"], candidate["boylam"]
        )
        rows.append({
            **candidate,
            "seed_merkezli_morfoloji_puani": morph.get("seed_merkezli_morfoloji_puani"),
            "seed_merkezli_morfoloji_seviyesi": morph.get("seed_merkezli_morfoloji_seviyesi"),
            "alarm": False,
            "saha_gorevi": False,
            "neden_diagnostik": (
                "13 Eylül sakin baseline ile en güncel kullanılabilir 17 Eylül sonrası aynı-yörünge sahnesinde "
                "doğrulanmış kazının lokal seed sınıfına benziyor; dört kritik FP çevresi bastırıldı."
            ),
        })
    rows.sort(
        key=lambda item: (
            -float(item.get("seed_merkezli_morfoloji_puani") or 0),
            -float(item.get("max_rgb_5x5") or 0),
        )
    )
    return rows[:SIMILAR_LIMIT]


def _region(region_key):
    pairs, pair_status = _candidate_pairs(region_key)
    if not pairs:
        return {
            "durum": pair_status,
            "bolge": satellite.REGIONS[region_key]["label"],
            "alarm": False,
            "saha_gorevi": False,
        }

    arrays = None
    visibility = None
    rejected = []
    for pair in pairs:
        candidate_arrays = _arrays_for_pair(region_key, pair)
        candidate_visibility = _reference_visibility(candidate_arrays)
        if candidate_visibility["kullanilabilir"]:
            arrays = candidate_arrays
            visibility = candidate_visibility
            break
        rejected.append({
            "post_onset_tarih": candidate_arrays["latest_date"],
            "post_onset_item": candidate_arrays["latest_item"],
            "neden": candidate_visibility["neden"],
            "referans_gorunurluk": candidate_visibility["referanslar"],
        })

    if arrays is None:
        return {
            "durum": "REFERANS_GORUNURLUK_YOK",
            "bolge": satellite.REGIONS[region_key]["label"],
            "cift_durumu": pair_status,
            "reddedilen_post_onset_sahneler": rejected,
            "alarm": False,
            "saha_gorevi": False,
        }

    checks = [
        _point_eval(point, arrays)
        for point in CRITICAL_POINTS
        if _in_bbox(point, arrays["bbox"])
    ]
    failures = [item for item in checks if not item["regresyon_uyumlu"]]
    true_row = next(
        (item for item in checks if item["sonuc"] == "DOGRULANMIS_KAZI"),
        None,
    )
    similar = _similar_candidates(
        region_key,
        arrays,
        true_row.get("seed_sinifi") if true_row else None,
    )
    return {
        "durum": "ok",
        "bolge": satellite.REGIONS[region_key]["label"],
        "cift_durumu": pair_status,
        "baseline_tarih": arrays["older_date"],
        "post_onset_tarih": arrays["latest_date"],
        "baseline_item": arrays["older_item"],
        "post_onset_item": arrays["latest_item"],
        "referans_gorunurluk": visibility,
        "reddedilen_post_onset_sahneler": rejected,
        "kritik_regresyon": checks,
        "regresyon_uyumsuz_sayisi": len(failures),
        "regresyon_uyumsuzluklari": failures,
        "gercek_kazi_seed_sinifi": true_row.get("seed_sinifi") if true_row else None,
        "benzer_diagnostik_adaylar": similar,
        "benzer_diagnostik_aday_sayisi": len(similar),
        "alarm": False,
        "saha_gorevi": False,
    }


def _self_check():
    assert len(CRITICAL_POINTS) == 5
    assert sum(p["sonuc"] == "DOGRULANMIS_KAZI" for p in CRITICAL_POINTS) == 1
    assert sum(p["sonuc"] == "YANLIS_POZITIF" for p in CRITICAL_POINTS) == 4
    assert _distance_m((38.3, 26.3), (38.3, 26.3)) == 0

    shape = (20, 20)
    bbox = [26.22, 38.18, 26.53, 38.43]
    arrays = {"bbox": bbox, "shape": shape, "valid": np.ones(shape, dtype=bool)}
    assert _reference_visibility(arrays)["kullanilabilir"] is True
    arrays["valid"][:] = False
    assert _reference_visibility(arrays)["kullanilabilir"] is False


def audit():
    _self_check()
    regions = {}
    for key in REGION_KEYS:
        try:
            regions[key] = _region(key)
        except Exception as exc:
            regions[key] = {
                "durum": "HATA",
                "bolge": satellite.REGIONS[key]["label"],
                "hata": f"{type(exc).__name__}: {exc}",
                "alarm": False,
                "saha_gorevi": False,
            }
    total_failures = sum(
        int(region.get("regresyon_uyumsuz_sayisi") or 0)
        for region in regions.values()
        if isinstance(region, dict)
    )
    return {
        "surum": 2,
        "amac": "13 Eylül sakin baseline ile en güncel kullanılabilir 17 Eylül sonrası Sentinel-2 müdahalesinde gerçek kazı/FP ayrımını ölçmek",
        "gercek_derinlik_olcumu": False,
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "baseline_tarihi": BASELINE_DATE,
        "post_onset_sonrasi": POST_ONSET_AFTER,
        "kritik_regresyon_ornek_sayisi": len(CRITICAL_POINTS),
        "toplam_regresyon_uyumsuz": total_failures,
        "bolgeler": regions,
        "not": (
            "Bu katman yalnız aynı MGRS ve aynı göreli yörüngede 13 Eylül→en güncel gerçekten kullanılabilir "
            "17 Eylül sonrası S2 çiftini kullanır. Kritik regresyon referansı bulut/no-data nedeniyle yeterli "
            "geçerli piksel taşımıyorsa o sahne reddedilir ve önceki uygun sahne denenir; uygun veri/geometri "
            "yoksa karşılaştırma üretilmez. Benzer adaylar diagnostiktir; SAR/ikinci tarih/saha kanıtı olmadan "
            "KONTROLE_GIT yapılmaz."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("post onset baseline excavation self-check: ok")
        return 0
    payload = audit()
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
