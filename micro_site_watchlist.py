"""Güçlü 150-249 m² MİKRO ŞANTİYE adaylarını Sentinel sahneleri arasında izler.

Bu katman alarm veya saha görevi üretmez. Amaç, bir Sentinel geçişinde güçlü
lokal/kompakt + temporal kanıt alan mikro adayın bir sonraki görüntüde değişim
maskesinden düşmesi halinde hafızadan tamamen silinmesini önlemektir. Aynı nokta
yeni ve bağımsız bir Sentinel sahnesinde tekrar güçlü görünürse bu yalnız
diagnostik ``tekrar_dogrulandi`` kanıtı olarak saklanır.

Ana 250 m² üretim eşiği değişmez; 150-249 m² kayıtlar hiçbir zaman bu dosyadan
operasyonel alarma veya kalıcı saha görevine terfi ettirilmez.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo


BASE = Path(__file__).resolve().parent
PRIORITY_REVIEW = BASE / "micro_site_footprint_priority_review.json"
TEMPORAL_REVIEW = BASE / "micro_site_temporal_review.json"
MICRO_AUDIT = BASE / "micro_site_audit.json"
OUTPUT = BASE / "micro_site_watchlist.json"

ISTANBUL = ZoneInfo("Europe/Istanbul")
MAIN_THRESHOLD_M2 = 250
MICRO_MIN_M2 = 150
MICRO_MAX_M2 = 249
MATCH_DISTANCE_M = 45
MAX_SCENE_HISTORY = 8

CURRENT_STATUS = "GUCLU_GUNCEL"
BACKGROUND_STATUS = "ARKA_PLAN_TAKIP"


def _load_json(path: Path) -> dict:
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


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _scene_lookup(temporal: dict, audit: dict) -> dict:
    """Bölge anahtarı -> son Sentinel item/tarih eşlemesi."""
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


def _strong_candidates(priority: dict) -> list[dict]:
    strong = priority.get("guclu_adaylar")
    if not isinstance(strong, list):
        strong = [
            item
            for item in (priority.get("adaylar") or [])
            if isinstance(item, dict)
            and item.get("mikro_footprint_guclu_diagnostik") is True
        ]

    cleaned = []
    for raw in strong:
        if not isinstance(raw, dict):
            continue
        latitude = _number(raw.get("enlem"))
        longitude = _number(raw.get("boylam"))
        area = _number(raw.get("alan_m2"))
        if latitude is None or longitude is None or area is None:
            continue
        if not (MICRO_MIN_M2 <= area <= MICRO_MAX_M2):
            continue
        if raw.get("alarm") is True or raw.get("saha_gorevi") is True:
            raise AssertionError("Mikro güçlü aday alarm/görev invariantını bozuyor.")
        item = dict(raw)
        item["enlem"] = round(latitude, 6)
        item["boylam"] = round(longitude, 6)
        item["alan_m2"] = int(round(area))
        cleaned.append(item)
    return cleaned


def _new_id(region: str, latitude: float, longitude: float) -> str:
    token = f"{region}|{latitude:.5f}|{longitude:.5f}".encode("utf-8")
    return "M" + hashlib.sha1(token).hexdigest()[:10].upper()


def _match_index(entries: list[dict], candidate: dict, used: set[int]) -> int | None:
    region = str(candidate.get("bolge") or "")
    point = (float(candidate["enlem"]), float(candidate["boylam"]))
    best = None
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
            best = index
            best_distance = distance
    return best


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


def build_watchlist(priority: dict, temporal: dict, audit: dict, previous: dict) -> dict:
    if int(priority.get("ana_uretim_esigi_m2") or MAIN_THRESHOLD_M2) != MAIN_THRESHOLD_M2:
        raise AssertionError("Ana üretim eşiği 250 m² değil.")
    interval = tuple(priority.get("mikro_aralik_m2") or (MICRO_MIN_M2, MICRO_MAX_M2))
    if interval != (MICRO_MIN_M2, MICRO_MAX_M2):
        raise AssertionError("Mikro bant 150-249 m² değil.")

    scene_by_region = _scene_lookup(temporal, audit)
    current = _strong_candidates(priority)

    previous_entries = previous.get("adaylar") or []
    entries = [dict(item) for item in previous_entries if isinstance(item, dict)]
    for entry in entries:
        entry["durum"] = BACKGROUND_STATUS
        entry["guncel_guclu"] = False
        entry["alarm"] = False
        entry["saha_gorevi"] = False

    used = set()
    for candidate in current:
        region = str(candidate.get("bolge") or "")
        scene = scene_by_region.get(region, {})
        scene_item = str(scene.get("son_item") or "").strip() or None
        scene_date = str(scene.get("son_tarih") or "").strip() or None
        match = _match_index(entries, candidate, used)

        if match is None:
            latitude = float(candidate["enlem"])
            longitude = float(candidate["boylam"])
            history = [scene_item] if scene_item else []
            entry = {
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
                "karar_nedeni": str(candidate.get("karar_nedeni") or ""),
                "bilesen_temporal_sinif": str(
                    candidate.get("bilesen_temporal_sinif") or ""
                ),
                "lokalite_sinifi": str(candidate.get("lokalite_sinifi") or ""),
                "yerel_kontrast_orani": candidate.get("yerel_kontrast_orani"),
            }
            entries.append(entry)
            used.add(len(entries) - 1)
            continue

        used.add(match)
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
                "tekrar_dogrulandi": len(history) >= 2,
                "farkli_sentinel_sahnesi_gorulme_sayisi": len(history),
                "sentinel_sahne_gecmisi": history,
                "son_guclu_gorulme_tarihi": scene_date
                or entry.get("son_guclu_gorulme_tarihi"),
                "son_guclu_sentinel_item": scene_item
                or entry.get("son_guclu_sentinel_item"),
                "yaklasik_mevki": str(
                    candidate.get("yaklasik_mevki")
                    or entry.get("yaklasik_mevki")
                    or "Mevki doğrulanmadı"
                ),
                "enlem": round(float(candidate["enlem"]), 6),
                "boylam": round(float(candidate["boylam"]), 6),
                "alan_m2": int(candidate["alan_m2"]),
                "harita": str(candidate.get("harita") or entry.get("harita") or ""),
                "karar_nedeni": str(
                    candidate.get("karar_nedeni") or entry.get("karar_nedeni") or ""
                ),
                "bilesen_temporal_sinif": str(
                    candidate.get("bilesen_temporal_sinif")
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
        entry.setdefault("ilk_gorulme_tarihi", scene_date)
        entry.setdefault(
            "mikro_iz_id",
            _new_id(region, float(candidate["enlem"]), float(candidate["boylam"])),
        )

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
    background_count = len(entries) - current_count

    return {
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": [MICRO_MIN_M2, MICRO_MAX_M2],
        "esleme_mesafesi_m": MATCH_DISTANCE_M,
        "amac": (
            "Güçlü lokal/kompakt + temporal mikro diagnostik adayları Sentinel "
            "sahneleri arasında hafızada tutmak; bir sonraki değişim çiftinde görünmese "
            "bile silmeyip arka planda izlemek."
        ),
        "uyari": (
            "Bu izleme listesi alarm veya saha görevi üretmez. İki farklı Sentinel "
            "sahnesinde tekrar güçlü görünme yalnız ek diagnostik kanıttır; 250 m² "
            "ana üretim eşiğini düşürmez."
        ),
        "guncel_guclu": current_count,
        "arka_plan_takip": background_count,
        "tekrar_dogrulanan": repeat_count,
        "adaylar": entries,
    }


def _semantic(payload: dict) -> dict:
    result = dict(payload)
    result.pop("olusturma", None)
    return result


def update_watchlist() -> tuple[dict, bool]:
    priority = _load_json(PRIORITY_REVIEW)
    if not priority:
        raise SystemExit("micro_site_footprint_priority_review.json bulunamadı.")
    temporal = _load_json(TEMPORAL_REVIEW)
    audit = _load_json(MICRO_AUDIT)
    previous = _load_json(OUTPUT)

    payload = build_watchlist(priority, temporal, audit, previous)
    if previous and _semantic(previous) == _semantic(payload):
        print("Mikro izleme listesinde anlamlı değişiklik yok.")
        return previous, False

    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Mikro izleme listesi güncellendi: "
        f"{payload['guncel_guclu']} güncel güçlü, "
        f"{payload['tekrar_dogrulanan']} tekrar doğrulanan, "
        f"{payload['arka_plan_takip']} arka plan."
    )
    return payload, True


def _self_check():
    priority = {
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "guclu_adaylar": [
            {
                "alarm": False,
                "saha_gorevi": False,
                "bolge": "uzunkuyu",
                "yaklasik_mevki": "Gülbahçe",
                "enlem": 38.3328,
                "boylam": 26.6456,
                "alan_m2": 200,
                "mikro_footprint_guclu_diagnostik": True,
                "karar_nedeni": "test",
            }
        ],
    }
    temporal_1 = {
        "bolgeler": {
            "uzunkuyu": {"son_item": "SCENE_A", "son_tarih": "03.09.2026"}
        }
    }
    first = build_watchlist(priority, temporal_1, {}, {})
    assert first["alarm"] is False and first["saha_gorevi"] is False
    assert first["guncel_guclu"] == 1
    assert first["tekrar_dogrulanan"] == 0
    assert first["adaylar"][0]["farkli_sentinel_sahnesi_gorulme_sayisi"] == 1

    same_scene = build_watchlist(priority, temporal_1, {}, first)
    assert same_scene["adaylar"][0]["farkli_sentinel_sahnesi_gorulme_sayisi"] == 1

    moved = json.loads(json.dumps(priority))
    moved["guclu_adaylar"][0]["enlem"] = 38.33295
    temporal_2 = {
        "bolgeler": {
            "uzunkuyu": {"son_item": "SCENE_B", "son_tarih": "08.09.2026"}
        }
    }
    second = build_watchlist(moved, temporal_2, {}, first)
    assert len(second["adaylar"]) == 1
    assert second["tekrar_dogrulanan"] == 1
    assert second["adaylar"][0]["farkli_sentinel_sahnesi_gorulme_sayisi"] == 2

    empty = dict(priority)
    empty["guclu_adaylar"] = []
    background = build_watchlist(empty, temporal_2, {}, second)
    assert background["guncel_guclu"] == 0
    assert background["arka_plan_takip"] == 1
    assert background["adaylar"][0]["durum"] == BACKGROUND_STATUS
    assert background["adaylar"][0]["alarm"] is False
    assert background["adaylar"][0]["saha_gorevi"] is False

    far = json.loads(json.dumps(priority))
    far["guclu_adaylar"][0]["enlem"] = 38.3340
    separate = build_watchlist(far, temporal_2, {}, first)
    assert len(separate["adaylar"]) == 2

    print("Mikro izleme listesi öz testi başarılı.")


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if not args.check_only:
        update_watchlist()


if __name__ == "__main__":
    main()
