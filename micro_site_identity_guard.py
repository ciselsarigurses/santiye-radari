"""150-249 m² MİKRO ŞANTİYE izlerinin mekânsal kimlik sürekliliğini denetler.

Mikro izleme listesi farklı Sentinel sahneleri arasında yaklaşık koordinat eşlemesi
kullanır. Bu katman mevcut 45 m izleme toleransını veya 250 m² ana alarm eşiğini
değiştirmez; yalnız ilk güçlü konumu saklar ve sonraki güçlü görünümün ilk konumdan
ne kadar saptığını ölçer. 25 m üzerindeki sapma, iki yakın fakat ayrı parselin tek iz
gibi yorumlanma riskidir ve diagnostik olarak işaretlenir.

Bu dosya alarm üretmez, saha görevi açmaz ve ``tekrar_dogrulandi`` kararını değiştirmez.
Ama gelecekte tekrar kanıtı operasyonel önceliklendirmede kullanılacaksa
``tekrar_dogrulama_mekansal_guvenli`` alanı ayrıca aranmalıdır.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


BASE = Path(__file__).resolve().parent
WATCHLIST = BASE / "micro_site_watchlist.json"
MAIN_THRESHOLD_M2 = 250
MICRO_MIN_M2 = 150
MICRO_MAX_M2 = 249
REPEAT_IDENTITY_MAX_DRIFT_M = 25.0


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _number(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def apply_identity_guard(payload: dict) -> tuple[dict, int, int]:
    if int(payload.get("ana_uretim_esigi_m2") or MAIN_THRESHOLD_M2) != MAIN_THRESHOLD_M2:
        raise AssertionError("Ana üretim eşiği 250 m² değil.")
    interval = tuple(payload.get("mikro_aralik_m2") or (MICRO_MIN_M2, MICRO_MAX_M2))
    if interval != (MICRO_MIN_M2, MICRO_MAX_M2):
        raise AssertionError("Mikro bant 150-249 m² değil.")
    if payload.get("alarm") is True or payload.get("saha_gorevi") is True:
        raise AssertionError("Mikro izleme listesi alarm/görev üretmemeli.")

    updated = json.loads(json.dumps(payload, ensure_ascii=False))
    entries = updated.get("adaylar") or []
    if not isinstance(entries, list):
        raise AssertionError("Mikro aday listesi geçersiz.")

    repeated = 0
    ambiguous = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        area = _number(entry.get("alan_m2"))
        latitude = _number(entry.get("enlem"))
        longitude = _number(entry.get("boylam"))
        if area is None or latitude is None or longitude is None:
            continue
        if not (MICRO_MIN_M2 <= area <= MICRO_MAX_M2):
            raise AssertionError("İzleme listesinde mikro bant dışı aday var.")
        if entry.get("alarm") is True or entry.get("saha_gorevi") is True:
            raise AssertionError("Mikro aday alarm/görev invariantını bozuyor.")

        first_lat = _number(entry.get("ilk_enlem"), latitude)
        first_lon = _number(entry.get("ilk_boylam"), longitude)
        entry["ilk_enlem"] = round(first_lat, 6)
        entry["ilk_boylam"] = round(first_lon, 6)

        drift = _distance_m(first_lat, first_lon, latitude, longitude)
        entry["ilk_konumdan_sapma_m"] = round(drift, 1)

        if bool(entry.get("tekrar_dogrulandi")):
            repeated += 1
            safe = drift <= REPEAT_IDENTITY_MAX_DRIFT_M
            entry["tekrar_dogrulama_mekansal_guvenli"] = safe
            entry["tekrar_dogrulama_mekansal_esik_m"] = REPEAT_IDENTITY_MAX_DRIFT_M
            if safe:
                entry["tekrar_dogrulama_mekansal_not"] = (
                    "Farklı Sentinel sahnelerindeki güçlü görünüm ilk mikro konumdan "
                    "25 m içinde; aynı küçük alan iziyle uyumlu."
                )
            else:
                ambiguous += 1
                entry["tekrar_dogrulama_mekansal_not"] = (
                    "Tekrar güçlü görünüm ilk mikro konumdan 25 m'den fazla kaymış. "
                    "Yakın ama ayrı parsel olasılığı nedeniyle tekrar kanıtı mekânsal "
                    "olarak belirsiz tutulmalı."
                )
        else:
            entry["tekrar_dogrulama_mekansal_guvenli"] = None
            entry["tekrar_dogrulama_mekansal_esik_m"] = REPEAT_IDENTITY_MAX_DRIFT_M
            entry["tekrar_dogrulama_mekansal_not"] = (
                "Henüz farklı Sentinel sahnesinde tekrar güçlü doğrulama yok."
            )

    return updated, repeated, ambiguous


def _semantic(payload: dict) -> dict:
    result = json.loads(json.dumps(payload, ensure_ascii=False))
    result.pop("olusturma", None)
    return result


def update_watchlist() -> tuple[dict, bool, int, int]:
    payload = _load(WATCHLIST)
    if not payload:
        raise SystemExit("micro_site_watchlist.json bulunamadı.")
    updated, repeated, ambiguous = apply_identity_guard(payload)
    if _semantic(updated) == _semantic(payload):
        print(
            "Mikro mekânsal kimlik korumasında anlamlı değişiklik yok: "
            f"{repeated} tekrar, {ambiguous} belirsiz."
        )
        return payload, False, repeated, ambiguous

    WATCHLIST.write_text(
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Mikro mekânsal kimlik alanları güncellendi: "
        f"{repeated} tekrar, {ambiguous} >25 m belirsiz tekrar."
    )
    return updated, True, repeated, ambiguous


def _self_check():
    base = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "adaylar": [
            {
                "mikro_iz_id": "MTEST",
                "alarm": False,
                "saha_gorevi": False,
                "bolge": "cesme",
                "enlem": 38.300000,
                "boylam": 26.350000,
                "alan_m2": 200,
                "tekrar_dogrulandi": False,
            }
        ],
    }
    initialized, repeated, ambiguous = apply_identity_guard(base)
    entry = initialized["adaylar"][0]
    assert repeated == 0 and ambiguous == 0
    assert entry["ilk_enlem"] == 38.3 and entry["ilk_boylam"] == 26.35
    assert entry["tekrar_dogrulama_mekansal_guvenli"] is None

    safe = json.loads(json.dumps(initialized))
    safe_entry = safe["adaylar"][0]
    safe_entry["tekrar_dogrulandi"] = True
    safe_entry["enlem"] = 38.300150  # yaklaşık 16.6 m kuzey
    safe_checked, repeated, ambiguous = apply_identity_guard(safe)
    safe_entry = safe_checked["adaylar"][0]
    assert repeated == 1 and ambiguous == 0
    assert safe_entry["tekrar_dogrulama_mekansal_guvenli"] is True
    assert 15 <= safe_entry["ilk_konumdan_sapma_m"] <= 18

    shifted = json.loads(json.dumps(initialized))
    shifted_entry = shifted["adaylar"][0]
    shifted_entry["tekrar_dogrulandi"] = True
    shifted_entry["enlem"] = 38.300300  # yaklaşık 33.2 m kuzey
    shifted_checked, repeated, ambiguous = apply_identity_guard(shifted)
    shifted_entry = shifted_checked["adaylar"][0]
    assert repeated == 1 and ambiguous == 1
    assert shifted_entry["tekrar_dogrulama_mekansal_guvenli"] is False
    assert shifted_entry["ilk_konumdan_sapma_m"] > REPEAT_IDENTITY_MAX_DRIFT_M

    print("Mikro mekânsal kimlik koruması öz testi başarılı.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if not args.check_only:
        update_watchlist()


if __name__ == "__main__":
    main()
