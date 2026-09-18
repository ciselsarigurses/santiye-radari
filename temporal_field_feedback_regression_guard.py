"""Saha gerçeklerini yalnız onları gözleyebilecek uydu tarihleriyle regresyona sokar.

Amaç: Örneğin 16 Eylül'de sahada doğrulanan bir temel kazısını, son Sentinel-2
sahnesi 15 Eylül iken "kaçırıldı" diye model aleyhine saymamak. Bu katman
operasyon adayı üretmez; yalnız diagnostik JSON'lardaki regresyon sayımını
zamansal olarak dürüst hale getirir. Negatif saha referansları (tarla/bahçe
yanlış pozitifleri) bastırma kalibrasyonu olarak değerlendirilmeye devam eder.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

import satellite


FEEDBACK_JSON = Path(__file__).with_name("manual_field_feedback.json")
REVIEW_PATHS = (
    Path(__file__).with_name("foundation_excavation_morphology_review.json"),
    Path(__file__).with_name("foundation_excavation_cross_evidence_review.json"),
)
POSITIVE_RESULT = "DOGRULANMIS_KAZI"


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _region_for_feedback(item):
    try:
        lat = float(item["enlem"])
        lon = float(item["boylam"])
    except (KeyError, TypeError, ValueError):
        return None
    for key, config in satellite.REGIONS.items():
        west, south, east, north = config["bbox"]
        if south <= lat <= north and west <= lon <= east:
            return key
    return None


def _positive_statuses(review, feedback_records):
    regions = review.get("bolgeler") if isinstance(review, dict) else {}
    regions = regions if isinstance(regions, dict) else {}
    statuses = {}
    for item in feedback_records:
        if str(item.get("sonuc") or "").upper() != POSITIVE_RESULT:
            continue
        item_id = item.get("id")
        validation_date = _parse_date(item.get("sonuc_tarihi"))
        region_key = _region_for_feedback(item)
        region = regions.get(region_key, {}) if region_key else {}
        scene_date = _parse_date(region.get("son_tarih")) if isinstance(region, dict) else None
        if not item_id or validation_date is None:
            continue
        temporal_valid = bool(scene_date is not None and scene_date >= validation_date)
        statuses[str(item_id)] = {
            "temporal_valid": temporal_valid,
            "regresyon_degerlendirildi": temporal_valid,
            "saha_dogrulama_tarihi": validation_date.isoformat(),
            "uydu_son_tarihi": scene_date.isoformat() if scene_date else None,
            "bolge_anahtari": region_key,
        }
    return statuses


def _patch_item(item, statuses):
    if not isinstance(item, dict):
        return False
    status = statuses.get(str(item.get("id")))
    if not status:
        return False
    item["temporal_valid"] = status["temporal_valid"]
    item["regresyon_degerlendirildi"] = status["regresyon_degerlendirildi"]
    item["saha_dogrulama_tarihi"] = status["saha_dogrulama_tarihi"]
    item["uydu_son_tarihi"] = status["uydu_son_tarihi"]
    if not status["temporal_valid"]:
        item["regresyon_uyumlu"] = None
        item["temporal_not"] = (
            "Saha doğrulaması son kullanılabilir uydu sahnesinden yeni; "
            "pozitif gerçek bu sahneye karşı regresyon başarısızlığı sayılmaz."
        )
    else:
        item.pop("temporal_not", None)
    return True


def _patch_morphology(review, statuses):
    regions = review.get("bolgeler") if isinstance(review, dict) else {}
    if not isinstance(regions, dict):
        return []
    ignored = []
    top_failures = []
    for region_key, region in regions.items():
        if not isinstance(region, dict):
            continue
        calibration = region.get("saha_kalibrasyonu")
        if not isinstance(calibration, list):
            continue
        for item in calibration:
            if _patch_item(item, statuses) and item.get("regresyon_degerlendirildi") is False:
                ignored.append(str(item.get("id")))
        failures = [
            item for item in calibration
            if isinstance(item, dict) and item.get("regresyon_uyumlu") is False
        ]
        region["regresyon_uyumsuz_sayisi"] = len(failures)
        region["regresyon_uyumsuz_ornekler"] = failures
        top_failures.extend({"bolge_anahtari": region_key, **item} for item in failures)
    review["toplam_regresyon_uyumsuz"] = len(top_failures)
    review["regresyon_uyumsuzluklari"] = top_failures
    return ignored


def _patch_cross_evidence(review, statuses):
    calibration = review.get("kalibrasyon_regresyonu") if isinstance(review, dict) else None
    if not isinstance(calibration, list):
        return []
    ignored = []
    for item in calibration:
        if _patch_item(item, statuses) and item.get("regresyon_degerlendirildi") is False:
            ignored.append(str(item.get("id")))
    failures = [
        item for item in calibration
        if isinstance(item, dict) and item.get("regresyon_uyumlu") is False
    ]
    review["toplam_regresyon_uyumsuz"] = len(failures)
    review["regresyon_uyumsuzluklari"] = failures
    morphology_failures = [
        item for item in calibration
        if isinstance(item, dict)
        and item.get("regresyon_degerlendirildi") is not False
        and item.get("morfoloji_regresyon_uyumlu") is False
    ]
    if "morfoloji_regresyon_uyumsuz_sayisi" in review:
        review["morfoloji_regresyon_uyumsuz_sayisi"] = len(morphology_failures)
    if "morfoloji_regresyon_uyumsuzluklari" in review:
        review["morfoloji_regresyon_uyumsuzluklari"] = morphology_failures
    return ignored


def _apply(path, feedback_records):
    review = _load_json(path)
    statuses = _positive_statuses(review, feedback_records)
    if path.name == "foundation_excavation_morphology_review.json":
        ignored = _patch_morphology(review, statuses)
    elif path.name == "foundation_excavation_cross_evidence_review.json":
        ignored = _patch_cross_evidence(review, statuses)
    else:
        ignored = []
    review["temporal_saha_regresyon_kapisi"] = {
        "aktif": True,
        "kural": "DOGRULANMIS_KAZI yalnız uydu_son_tarihi >= saha_dogrulama_tarihi ise regresyona girer",
        "zamansal_olarak_degerlendirilmemis_pozitifler": sorted(set(ignored)),
    }
    path.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return sorted(set(ignored))


def _self_check():
    assert _parse_date("15.09.2026") == date(2026, 9, 15)
    assert _parse_date("2026-09-16") == date(2026, 9, 16)
    fake = {
        "bolgeler": {
            "cesme": {"son_tarih": "15.09.2026"},
            "uzunkuyu": {"son_tarih": "15.09.2026"},
        }
    }
    feedback = [{
        "id": "FN-TEST",
        "sonuc": POSITIVE_RESULT,
        "sonuc_tarihi": "2026-09-16",
        "enlem": 38.341846,
        "boylam": 26.432861,
    }]
    statuses = _positive_statuses(fake, feedback)
    assert statuses["FN-TEST"]["temporal_valid"] is False, statuses
    sample = {"id": "FN-TEST", "sonuc": POSITIVE_RESULT, "regresyon_uyumlu": False}
    _patch_item(sample, statuses)
    assert sample["regresyon_uyumlu"] is None, sample
    assert sample["regresyon_degerlendirildi"] is False, sample


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("temporal field feedback regression guard self-check: ok")
        return
    feedback = _load_json(FEEDBACK_JSON).get("kayitlar", [])
    feedback = [item for item in feedback if isinstance(item, dict)]
    summary = {}
    for path in REVIEW_PATHS:
        if path.exists():
            summary[path.name] = _apply(path, feedback)
    print(json.dumps({"temporal_guard": summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
