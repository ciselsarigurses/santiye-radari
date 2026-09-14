"""Sentinel-1 SAR harita snapshotini gecici kaynak hatalarinda korur.

Saatlik SAR canli olcumu veri saglayicisi nedeniyle gecici olarak bos donerse, daha
once yayinlanmis diagnostik/arka-plan noktalarini "0 aday" sanip silmeyiz. Gercek
bir bos sonuc ile kaynak erisim hatasini ayirir. Alarm veya saha gorevi uretmez;
250 m2 ana esigi ve 150-249 m2 MIKRO politikasi degismez.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

MAIN_MIN_M2 = 250
MICRO_RANGE_M2 = [150, 249]


def _load(path: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON okunamadi: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON nesnesi bekleniyordu: {path}")
    return value


def _count(payload: dict) -> int:
    value = payload.get("feature_count")
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        features = payload.get("features")
        return len(features) if isinstance(features, list) else 0


def _policy_ok(payload: dict) -> bool:
    return (
        payload.get("alarm") is False
        and payload.get("saha_gorevi") is False
        and payload.get("ana_sentinel_esigi_m2") == MAIN_MIN_M2
        and payload.get("mikro_aralik_m2") == MICRO_RANGE_M2
    )


def _transient_rtc_failure(rtc: dict) -> tuple[bool, list[str]]:
    failures = []
    for region in rtc.get("bolgeler") or []:
        if not isinstance(region, dict):
            continue
        status = str(region.get("durum") or "").strip().upper()
        error = str(region.get("hata") or "").strip().upper()
        if "GECICI_HATA" in status or "KAYNAK_ERISILEMEDI" in status or error == "HTTPERROR":
            failures.append(str(region.get("bolge") or "bilinmeyen"))
    return bool(failures), sorted(set(failures))


def choose_snapshot(rtc: dict, candidate: dict, current: dict) -> tuple[dict, dict]:
    if not _policy_ok(candidate):
        raise ValueError("Aday SAR harita snapshotinda 250 m2 / MIKRO / alarm politikasi bozuldu")
    if current and not _policy_ok(current):
        raise ValueError("Mevcut SAR harita snapshotinda koruyucu politika bozuk")

    transient, regions = _transient_rtc_failure(rtc)
    candidate_count = _count(candidate)
    current_count = _count(current)

    preserve = transient and candidate_count == 0 and current_count > 0
    selected = current if preserve else candidate
    decision = {
        "durum": "ONCEKI_SNAPSHOT_KORUNDU" if preserve else "YENI_SNAPSHOT_YAYINLANABILIR",
        "gecici_rtc_kaynak_hatasi": transient,
        "hata_bolgeleri": regions,
        "aday_feature_count": candidate_count,
        "mevcut_feature_count": current_count,
        "yayin_feature_count": _count(selected),
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": MAIN_MIN_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "not": (
            "Gecici SAR kaynak hatasi bos haritayi gercek bos sonuc gibi yayinlamaz; "
            "son gecerli diagnostik/arka-plan snapshot korunur. Yeni canli veri geldiginde normal yayin devam eder."
        ),
    }
    return selected, decision


def _base_map(count: int) -> dict:
    return {
        "type": "FeatureCollection",
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "feature_count": count,
        "features": [{"type": "Feature"}] * count,
    }


def _self_check() -> None:
    failed_rtc = {
        "bolgeler": [
            {"bolge": "cesme", "durum": "RTC_KAYNAK_GECICI_HATA", "hata": "HTTPError"},
            {"bolge": "uzunkuyu", "durum": "RTC_KAYNAK_GECICI_HATA", "hata": "HTTPError"},
            {"bolge": "gulbahce", "durum": "RTC_KAYNAK_GECICI_HATA", "hata": "HTTPError"},
        ]
    }
    good_rtc = {"bolgeler": [{"bolge": "cesme", "durum": "RTC_OK"}]}

    selected, decision = choose_snapshot(failed_rtc, _base_map(0), _base_map(20))
    assert _count(selected) == 20
    assert decision["durum"] == "ONCEKI_SNAPSHOT_KORUNDU"
    assert decision["hata_bolgeleri"] == ["cesme", "gulbahce", "uzunkuyu"]

    selected, decision = choose_snapshot(good_rtc, _base_map(0), _base_map(20))
    assert _count(selected) == 0
    assert decision["durum"] == "YENI_SNAPSHOT_YAYINLANABILIR"

    selected, decision = choose_snapshot(failed_rtc, _base_map(4), _base_map(20))
    assert _count(selected) == 4
    assert decision["durum"] == "YENI_SNAPSHOT_YAYINLANABILIR"

    selected, decision = choose_snapshot(failed_rtc, _base_map(0), _base_map(0))
    assert _count(selected) == 0
    assert decision["durum"] == "YENI_SNAPSHOT_YAYINLANABILIR"

    print("Sentinel-1 RTC snapshot guard self-check OK")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rtc")
    parser.add_argument("--candidate")
    parser.add_argument("--current")
    parser.add_argument("--output")
    parser.add_argument("--self-check-only", action="store_true")
    args = parser.parse_args()

    if args.self_check_only:
        _self_check()
        return
    if not all([args.rtc, args.candidate, args.current, args.output]):
        parser.error("--rtc, --candidate, --current ve --output gerekli")

    selected, decision = choose_snapshot(
        _load(args.rtc),
        _load(args.candidate),
        _load(args.current),
    )
    Path(args.output).write_text(
        json.dumps(selected, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(decision, ensure_ascii=False))


if __name__ == "__main__":
    main()
