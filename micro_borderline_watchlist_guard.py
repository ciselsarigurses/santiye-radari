"""Spektral sınırda ama temporal + lokal güçlü MİKRO adayları izleme hafızasına bağlar.

Normal mikro footprint izleme katmanı yalnız ana spektral kapıyı geçen adayları kalıcı
hafızaya alır. Bu ek koruma, ana eşiği değiştirmeden tek spektral metriği dar marjla
kaçırdığı halde ``SINIR_TEMPORAL_LOKAL_GUCLU`` kanıtı alan 150-249 m² adayları aynı
watchlist içinde tutar.

Bu katman alarm veya saha görevi üretmez, 15 Eylül otomatik terfisi yapmaz ve 250 m²
ana üretim eşiğini değiştirmez. Aynı bölgede mevcut bir mikro izle 45 m içinde eşleşen
sınır adayı yeni kayıt açmaz; yalnız ek diagnostik kanıt olarak işaretlenir. Aynı
turda tek bir iz en fazla bir güçlü adayla eşleşir; yakın iki ayrı mikro değişim tek
iz üstüne yazılarak koordinat kimliğini kaybetmez.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo


BASE = Path(__file__).resolve().parent
WATCHLIST = BASE / "micro_site_watchlist.json"
BORDERLINE_REVIEW = BASE / "micro_spectral_borderline_review.json"
TEMPORAL_REVIEW = BASE / "micro_site_temporal_review.json"
MICRO_AUDIT = BASE / "micro_site_audit.json"

ISTANBUL = ZoneInfo("Europe/Istanbul")
MAIN_THRESHOLD_M2 = 250
MICRO_MIN_M2 = 150
MICRO_MAX_M2 = 249
MATCH_DISTANCE_M = 45.0
MAX_SCENE_HISTORY = 8
SOURCE = "SPEKTRAL_SINIR_TEMPORAL_LOKAL"
CURRENT_STATUS = "GUCLU_GUNCEL"


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


def _distance_m(first, second) -> float:
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _scene_lookup(temporal: dict, audit: dict) -> dict:
    result = {}
    for payload in (temporal, audit):
        regions = payload.get("bolgeler") or {}
        if not isinstance(regions, dict):
            continue
        for region_key, region in regions.items():
            if not isinstance(region, dict):
                continue
            item = str(region.get("son_item") or "").strip()
            date = str(region.get("son_tarih") or "").strip()
            if item or date:
                result.setdefault(
                    str(region_key),
                    {"son_item": item or None, "son_tarih": date or None},
                )
    return result


def _strong_borderline_candidates(payload: dict) -> list[dict]:
    if not payload:
        return []

    if int(payload.get("ana_uretim_esigi_m2") or MAIN_THRESHOLD_M2) != MAIN_THRESHOLD_M2:
        raise AssertionError("Spektral sınır katmanında ana üretim eşiği 250 m² değil.")
    interval = tuple(payload.get("mikro_aralik_m2") or (MICRO_MIN_M2, MICRO_MAX_M2))
    if interval != (MICRO_MIN_M2, MICRO_MAX_M2):
        raise AssertionError("Spektral sınır katmanında mikro bant 150-249 m² değil.")

    selected = []
    seen = set()
    regions = payload.get("bolgeler") or {}
    if not isinstance(regions, dict):
        return selected

    for region_key, region in regions.items():
        if not isinstance(region, dict):
            continue
        for raw in region.get("adaylar") or []:
            if not isinstance(raw, dict):
                continue
            if raw.get("sinir_temporal_lokal_guclu") is not True:
                continue

            latitude = _number(raw.get("enlem"))
            longitude = _number(raw.get("boylam"))
            area = _number(raw.get("alan_m2"))
            if latitude is None or longitude is None or area is None:
                continue
            if not (MICRO_MIN_M2 <= area <= MICRO_MAX_M2):
                continue
            if raw.get("alarm") is True or raw.get("saha_gorevi") is True:
                raise AssertionError("Spektral sınır güçlü mikro aday alarm/görev üretiyor.")
            if raw.get("otomatik_alarm") is True or raw.get("otomatik_saha_gorevi") is True:
                raise AssertionError("Spektral sınır güçlü mikro aday otomatik alarm/görev üretiyor.")
            if raw.get("15_eylul_otomatik_terfi") is True:
                raise AssertionError("Spektral sınır güçlü mikro aday 15 Eylül otomatik terfi üretiyor.")

            item = dict(raw)
            item["bolge"] = str(raw.get("bolge") or region_key)
            item["enlem"] = round(latitude, 6)
            item["boylam"] = round(longitude, 6)
            item["alan_m2"] = int(round(area))
            item["mikro_kaynak"] = SOURCE
            identity = (
                item["bolge"],
                round(item["enlem"], 5),
                round(item["boylam"], 5),
            )
            if identity in seen:
                continue
            seen.add(identity)
            selected.append(item)

    return selected


def _history(entry: dict) -> list[str]:
    values = entry.get("sentinel_sahne_gecmisi") or []
    if not isinstance(values, list):
        return []
    cleaned = []
    for value in values:
        token = str(value or "").strip()
        if token and token not in cleaned:
            cleaned.append(token)
    return cleaned[-MAX_SCENE_HISTORY:]


def _match_index(
    entries: list[dict],
    candidate: dict,
    used: set[int],
) -> tuple[int | None, float | None]:
    region = str(candidate.get("bolge") or "")
    point = (float(candidate["enlem"]), float(candidate["boylam"]))
    best_index = None
    best_distance = None

    for index, entry in enumerate(entries):
        if index in used:
            continue
        if str(entry.get("bolge") or "") != region:
            continue
        latitude = _number(entry.get("enlem"))
        longitude = _number(entry.get("boylam"))
        if latitude is None or longitude is None:
            continue
        distance = _distance_m(point, (latitude, longitude))
        if distance > MATCH_DISTANCE_M:
            continue
        if best_distance is None or distance < best_distance:
            best_index = index
            best_distance = distance

    return best_index, best_distance


def _new_id(region: str, latitude: float, longitude: float) -> str:
    token = f"borderline|{region}|{latitude:.5f}|{longitude:.5f}".encode("utf-8")
    return "MB" + hashlib.sha1(token).hexdigest()[:9].upper()


def apply_borderline_guard(
    watchlist: dict,
    borderline: dict,
    temporal: dict,
    audit: dict,
) -> tuple[dict, int, int]:
    if int(watchlist.get("ana_uretim_esigi_m2") or 0) != MAIN_THRESHOLD_M2:
        raise AssertionError("Mikro izleme listesinde ana üretim eşiği 250 m² değil.")
    interval = tuple(watchlist.get("mikro_aralik_m2") or ())
    if interval != (MICRO_MIN_M2, MICRO_MAX_M2):
        raise AssertionError("Mikro izleme listesinde mikro bant 150-249 m² değil.")
    if watchlist.get("alarm") is True or watchlist.get("saha_gorevi") is True:
        raise AssertionError("Mikro izleme listesi alarm/görev üretmemeli.")

    updated = json.loads(json.dumps(watchlist, ensure_ascii=False))
    entries = updated.get("adaylar") or []
    if not isinstance(entries, list):
        raise AssertionError("Mikro izleme aday listesi geçersiz.")

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry["alarm"] = False
        entry["saha_gorevi"] = False

    candidates = _strong_borderline_candidates(borderline)
    scene_by_region = _scene_lookup(temporal, audit)
    added = 0
    matched = 0
    used = set()

    for candidate in candidates:
        region = str(candidate.get("bolge") or "")
        scene = scene_by_region.get(region, {})
        scene_item = str(scene.get("son_item") or "").strip() or None
        scene_date = str(scene.get("son_tarih") or "").strip() or None

        match, distance = _match_index(entries, candidate, used)
        if match is not None:
            used.add(match)
            matched += 1
            entry = entries[match]
            history = _history(entry)
            if scene_item and scene_item not in history:
                history.append(scene_item)
            history = history[-MAX_SCENE_HISTORY:]

            entry.update(
                {
                    "alarm": False,
                    "saha_gorevi": False,
                    "durum": CURRENT_STATUS,
                    "guncel_guclu": True,
                    "farkli_sentinel_sahnesi_gorulme_sayisi": len(history),
                    "sentinel_sahne_gecmisi": history,
                    "tekrar_dogrulandi": len(history) >= 2,
                    "son_guclu_gorulme_tarihi": scene_date
                    or entry.get("son_guclu_gorulme_tarihi"),
                    "son_guclu_sentinel_item": scene_item
                    or entry.get("son_guclu_sentinel_item"),
                    "sinir_temporal_lokal_ek_kanit": True,
                    "sinir_ek_kanit_esleme_mesafe_m": round(float(distance or 0.0), 1),
                }
            )
            if entry.get("mikro_kaynak") == SOURCE:
                entry.update(
                    {
                        "yaklasik_mevki": str(
                            candidate.get("yaklasik_mevki")
                            or entry.get("yaklasik_mevki")
                            or "Mevki doğrulanmadı"
                        ),
                        "enlem": candidate["enlem"],
                        "boylam": candidate["boylam"],
                        "alan_m2": candidate["alan_m2"],
                        "harita": str(candidate.get("harita") or entry.get("harita") or ""),
                        "karar_nedeni": str(
                            candidate.get("sinir_diagnostik_nedeni")
                            or entry.get("karar_nedeni")
                            or ""
                        ),
                        "bilesen_temporal_sinif": str(
                            candidate.get("temporal_sinif")
                            or entry.get("bilesen_temporal_sinif")
                            or ""
                        ),
                        "lokalite_sinifi": str(
                            candidate.get("lokalite_sinifi")
                            or entry.get("lokalite_sinifi")
                            or ""
                        ),
                        "yerel_kontrast_orani": candidate.get(
                            "yerel_kontrast_orani",
                            entry.get("yerel_kontrast_orani"),
                        ),
                    }
                )
            continue

        latitude = float(candidate["enlem"])
        longitude = float(candidate["boylam"])
        history = [scene_item] if scene_item else []
        entries.append(
            {
                "mikro_iz_id": _new_id(region, latitude, longitude),
                "alarm": False,
                "saha_gorevi": False,
                "durum": CURRENT_STATUS,
                "guncel_guclu": True,
                "tekrar_dogrulandi": False,
                "farkli_sentinel_sahnesi_gorulme_sayisi": len(history),
                "sentinel_sahne_gecmisi": history,
                "ilk_gorulme_tarihi": scene_date,
                "son_guclu_gorulme_tarihi": scene_date,
                "son_guclu_sentinel_item": scene_item,
                "bolge": region,
                "yaklasik_mevki": str(
                    candidate.get("yaklasik_mevki") or "Mevki doğrulanmadı"
                ),
                "enlem": round(latitude, 6),
                "boylam": round(longitude, 6),
                "alan_m2": int(candidate["alan_m2"]),
                "harita": str(candidate.get("harita") or ""),
                "karar_nedeni": str(candidate.get("sinir_diagnostik_nedeni") or ""),
                "bilesen_temporal_sinif": str(candidate.get("temporal_sinif") or ""),
                "lokalite_sinifi": str(candidate.get("lokalite_sinifi") or ""),
                "yerel_kontrast_orani": candidate.get("yerel_kontrast_orani"),
                "mikro_kaynak": SOURCE,
                "sinir_temporal_lokal_ek_kanit": True,
                "sinir_ek_kanit_esleme_mesafe_m": 0.0,
            }
        )
        used.add(len(entries) - 1)
        added += 1

    entries.sort(
        key=lambda item: (
            0 if item.get("guncel_guclu") else 1,
            0 if item.get("tekrar_dogrulandi") else 1,
            str(item.get("bolge") or ""),
            str(item.get("mikro_iz_id") or ""),
        )
    )

    current_count = sum(bool(item.get("guncel_guclu")) for item in entries)
    repeat_count = sum(bool(item.get("tekrar_dogrulandi")) for item in entries)
    updated["adaylar"] = entries
    updated["guncel_guclu"] = current_count
    updated["arka_plan_takip"] = len(entries) - current_count
    updated["tekrar_dogrulanan"] = repeat_count
    updated["sinir_temporal_lokal_guclu_girdi"] = len(candidates)
    updated["sinir_watchlist_yeni"] = added
    updated["sinir_watchlist_eslesen"] = matched
    updated["sinir_watchlist_kurali"] = (
        "Normal spektral eşiği değiştirmeden yalnız SINIR_TEMPORAL_LOKAL_GUCLU "
        "150-249 m² adaylar kalıcı mikro hafızaya eklenir; alarm/görev/15 Eylül "
        "otomatik terfisi yoktur. Aynı bölgede 45 m içindeki mevcut iz yinelenmez; "
        "aynı turda tek iz en fazla bir güçlü adayla eşleşir."
    )
    if added or matched or updated.get("olusturma"):
        updated["olusturma"] = datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z")

    return updated, added, matched


def _semantic(payload: dict) -> dict:
    result = json.loads(json.dumps(payload, ensure_ascii=False))
    result.pop("olusturma", None)
    return result


def update_watchlist() -> tuple[dict, bool, int, int]:
    watchlist = _load(WATCHLIST)
    if not watchlist:
        raise SystemExit("micro_site_watchlist.json bulunamadı.")
    borderline = _load(BORDERLINE_REVIEW)
    temporal = _load(TEMPORAL_REVIEW)
    audit = _load(MICRO_AUDIT)

    updated, added, matched = apply_borderline_guard(
        watchlist, borderline, temporal, audit
    )
    if _semantic(updated) == _semantic(watchlist):
        print(
            "Spektral sınır mikro izleme hafızasında anlamlı değişiklik yok: "
            f"{added} yeni, {matched} eşleşen."
        )
        return watchlist, False, added, matched

    WATCHLIST.write_text(
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Spektral sınır mikro izleme hafızasına bağlandı: "
        f"{added} yeni, {matched} mevcut izle eşleşen, "
        f"{updated['guncel_guclu']} toplam güncel güçlü."
    )
    return updated, True, added, matched


def _self_check():
    base = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "adaylar": [
            {
                "mikro_iz_id": "MNORMAL",
                "alarm": False,
                "saha_gorevi": False,
                "durum": "GUCLU_GUNCEL",
                "guncel_guclu": True,
                "tekrar_dogrulandi": False,
                "farkli_sentinel_sahnesi_gorulme_sayisi": 1,
                "sentinel_sahne_gecmisi": ["SCENE_A"],
                "bolge": "cesme",
                "enlem": 38.300000,
                "boylam": 26.350000,
                "alan_m2": 200,
            }
        ],
    }
    temporal = {
        "bolgeler": {
            "cesme": {"son_item": "SCENE_A", "son_tarih": "03.09.2026"},
            "uzunkuyu": {"son_item": "SCENE_A", "son_tarih": "03.09.2026"},
        }
    }
    border = {
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "bolgeler": {
            "uzunkuyu": {
                "adaylar": [
                    {
                        "alarm": False,
                        "saha_gorevi": False,
                        "otomatik_alarm": False,
                        "otomatik_saha_gorevi": False,
                        "15_eylul_otomatik_terfi": False,
                        "sinir_temporal_lokal_guclu": True,
                        "bolge": "uzunkuyu",
                        "yaklasik_mevki": "Gülbahçe",
                        "enlem": 38.3272,
                        "boylam": 26.6466,
                        "alan_m2": 200,
                        "temporal_sinif": "ANI_BASLANGIC_DESTEGI",
                        "lokalite_sinifi": "LOKAL_KOMPAKT_TEMPORAL_DESTEK",
                        "yerel_kontrast_orani": 5.0,
                        "sinir_diagnostik_nedeni": "test",
                    }
                ]
            }
        },
    }

    added_payload, added, matched = apply_borderline_guard(base, border, temporal, {})
    assert added == 1 and matched == 0
    assert added_payload["guncel_guclu"] == 2
    assert len(added_payload["adaylar"]) == 2
    border_entry = next(
        item for item in added_payload["adaylar"] if item.get("mikro_kaynak") == SOURCE
    )
    assert border_entry["alarm"] is False and border_entry["saha_gorevi"] is False
    assert border_entry["farkli_sentinel_sahnesi_gorulme_sayisi"] == 1

    same_payload, added, matched = apply_borderline_guard(
        added_payload, border, temporal, {}
    )
    assert added == 0 and matched == 1
    assert len(same_payload["adaylar"]) == 2
    assert same_payload["guncel_guclu"] == 2

    near = json.loads(json.dumps(border))
    near_candidate = near["bolgeler"]["uzunkuyu"]["adaylar"][0]
    near_candidate["bolge"] = "cesme"
    near_candidate["enlem"] = 38.30005
    near_candidate["boylam"] = 26.35000
    near_payload, added, matched = apply_borderline_guard(base, near, temporal, {})
    assert added == 0 and matched == 1
    assert len(near_payload["adaylar"]) == 1
    assert near_payload["adaylar"][0]["sinir_temporal_lokal_ek_kanit"] is True

    # Aynı sahnede birbirine yakın iki ayrı güçlü sınır aday tek mevcut izi paylaşmamalı.
    close_pair = json.loads(json.dumps(border))
    first = close_pair["bolgeler"]["uzunkuyu"]["adaylar"][0]
    first["bolge"] = "cesme"
    first["enlem"] = 38.30005
    first["boylam"] = 26.35000
    second = dict(first)
    second["enlem"] = 38.30030
    second["boylam"] = 26.35000
    close_pair["bolgeler"]["uzunkuyu"]["adaylar"] = [first, second]
    pair_payload, added, matched = apply_borderline_guard(base, close_pair, temporal, {})
    assert added == 1 and matched == 1
    assert len(pair_payload["adaylar"]) == 2
    pair_points = {
        (round(float(item["enlem"]), 5), round(float(item["boylam"]), 5))
        for item in pair_payload["adaylar"]
    }
    assert len(pair_points) == 2

    weak = json.loads(json.dumps(border))
    weak["bolgeler"]["uzunkuyu"]["adaylar"][0]["sinir_temporal_lokal_guclu"] = False
    weak_payload, added, matched = apply_borderline_guard(base, weak, temporal, {})
    assert added == 0 and matched == 0
    assert len(weak_payload["adaylar"]) == 1

    unsafe = json.loads(json.dumps(border))
    unsafe["bolgeler"]["uzunkuyu"]["adaylar"][0]["15_eylul_otomatik_terfi"] = True
    try:
        apply_borderline_guard(base, unsafe, temporal, {})
    except AssertionError:
        pass
    else:
        raise AssertionError("15 Eylül otomatik terfi invariantı korunmadı.")

    print("Spektral sınır mikro watchlist koruması öz testi başarılı.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if not args.check_only:
        update_watchlist()


if __name__ == "__main__":
    main()
