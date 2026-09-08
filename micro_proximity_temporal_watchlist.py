"""Yakın-ana MİKRO temporal kanıtını Sentinel sahneleri arasında kalıcı tutar.

Normal MİKRO kısa liste, 250 m²+ ana adaya çok yakın parçaları aynı büyük olayın
parçası olabileceği için operasyonel listeden bastırır. Ayrı temporal probe bu
parçalardaki güçlü ani başlangıç/devam kanıtını ölçer; ancak probe çıktısı yeni
Sentinel sahnesinde yeniden üretildiğinde eski güçlü iz kaybolabilir.

Bu dosya yalnız bu dar diagnostik sınıf için kalıcı hafıza tutar:
- 150-249 m²;
- ana 250 m²+ adaya yakın ve normal operasyonel MİKRO kısa listede değil;
- ana spektral/kompakt kapıyı geçmiş;
- üç-sahne geçerli veri oranı yeterli;
- ani başlangıç veya devam eden hareket desteği var;
- geniş hareket kümesi ya da önceki dönemde hareketli zemin riski yok.

Çıktı alarm/saha görevi üretmez ve 250 m² ana eşiği değiştirmez. Farklı Sentinel
sahnesinde aynı küçük alanda tekrar güçlü kanıt yalnız diagnostik tekrar kanıtıdır.
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
PROBE_FILE = BASE / "micro_proximity_temporal_probe.json"
OUTPUT_FILE = BASE / "micro_proximity_temporal_watchlist.json"

ISTANBUL = ZoneInfo("Europe/Istanbul")
MAIN_THRESHOLD_M2 = 250
MICRO_MIN_M2 = 150
MICRO_MAX_M2 = 249
MATCH_DISTANCE_M = 25.0
MIN_VALID_RATIO = 0.75
MAX_SCENE_HISTORY = 8
SOURCE = "YAKIN_ANA_TEMPORAL_PROBE"
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


def _distance_m(first, second) -> float:
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _new_id(region: str, latitude: float, longitude: float) -> str:
    token = f"proximity|{region}|{latitude:.5f}|{longitude:.5f}".encode("utf-8")
    return "MP" + hashlib.sha1(token).hexdigest()[:9].upper()


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


def _validate_probe_header(probe: dict) -> None:
    if not isinstance(probe, dict):
        raise AssertionError("Yakın-ana temporal probe payload geçersiz.")
    if probe.get("alarm") is True or probe.get("saha_gorevi") is True:
        raise AssertionError("Yakın-ana temporal probe alarm/görev üretmemeli.")
    if int(_number(probe.get("ana_uretim_esigi_m2"), 0) or 0) != MAIN_THRESHOLD_M2:
        raise AssertionError("Ana üretim eşiği 250 m² değil.")
    interval = tuple(probe.get("mikro_aralik_m2") or ())
    if interval != (MICRO_MIN_M2, MICRO_MAX_M2):
        raise AssertionError("Mikro bant 150-249 m² değil.")
    if probe.get("operasyonel_kisa_listeye_etki") is not False:
        raise AssertionError("Yakın-ana temporal probe operasyonel kısa listeyi etkilememeli.")


def _strong_probe_candidates(probe: dict) -> list[dict]:
    _validate_probe_header(probe)
    selected = []
    seen = set()
    regions = probe.get("bolgeler") or {}
    if not isinstance(regions, dict):
        return selected

    for region_key, region in regions.items():
        if not isinstance(region, dict):
            continue
        scene_item = str(region.get("son_item") or "").strip()
        scene_date = str(region.get("son_tarih") or "").strip()
        if not scene_item or not scene_date:
            continue

        for raw in region.get("adaylar") or []:
            if not isinstance(raw, dict):
                continue
            latitude = _number(raw.get("enlem"))
            longitude = _number(raw.get("boylam"))
            area = _number(raw.get("alan_m2"))
            valid_ratio = _number(raw.get("uc_sahne_gecerli_oran"), 0.0)
            if latitude is None or longitude is None or area is None:
                continue
            if not (MICRO_MIN_M2 <= area <= MICRO_MAX_M2):
                continue
            if raw.get("alarm") is True or raw.get("saha_gorevi") is True:
                raise AssertionError("Yakın-ana MİKRO probe adayı alarm/görev üretiyor.")
            if raw.get("temporal_probe_only") is not True:
                continue
            if raw.get("operasyonel_kisa_liste") is not False:
                continue
            if raw.get("spektral_kisa_liste_kapisi") is not True:
                continue
            if raw.get("ana_adaya_yakin") is not True:
                continue
            if raw.get("genis_hareket_kumesi_riski") is True:
                continue
            if raw.get("onceki_zemin_hareketli_riski") is True:
                continue
            if valid_ratio is None or valid_ratio < MIN_VALID_RATIO:
                continue
            temporal_support = (
                raw.get("ani_baslangic_destegi") is True
                or raw.get("devam_eden_hareket_destegi") is True
            )
            if not temporal_support:
                continue

            item = dict(raw)
            item.update(
                {
                    "bolge": str(raw.get("bolge") or region_key),
                    "enlem": round(latitude, 6),
                    "boylam": round(longitude, 6),
                    "alan_m2": int(round(area)),
                    "mikro_kaynak": SOURCE,
                    "mikro_kaynak_sentinel_item": scene_item,
                    "mikro_kaynak_sentinel_tarihi": scene_date,
                }
            )
            identity = (
                item["bolge"],
                round(item["enlem"], 5),
                round(item["boylam"], 5),
                item["mikro_kaynak_sentinel_item"],
            )
            if identity in seen:
                continue
            seen.add(identity)
            selected.append(item)
    return selected


def _match_index(entries: list[dict], candidate: dict, used: set[int]):
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


def build_watchlist(probe: dict, previous: dict) -> dict:
    current = _strong_probe_candidates(probe)

    previous_entries = previous.get("adaylar") or []
    entries = [dict(item) for item in previous_entries if isinstance(item, dict)]
    for entry in entries:
        entry["durum"] = BACKGROUND_STATUS
        entry["guncel_guclu"] = False
        entry["alarm"] = False
        entry["saha_gorevi"] = False
        entry["otomatik_alarm"] = False
        entry["otomatik_saha_gorevi"] = False
        entry["15_eylul_otomatik_terfi"] = False
        entry["bagimsiz_santiye_dogrulanmadi"] = True

    used = set()
    for candidate in current:
        region = str(candidate["bolge"])
        scene_item = str(candidate["mikro_kaynak_sentinel_item"])
        scene_date = str(candidate["mikro_kaynak_sentinel_tarihi"])
        match, distance = _match_index(entries, candidate, used)

        if match is None:
            latitude = float(candidate["enlem"])
            longitude = float(candidate["boylam"])
            entry = {
                "mikro_iz_id": _new_id(region, latitude, longitude),
                "mikro_kaynak": SOURCE,
                "alarm": False,
                "saha_gorevi": False,
                "otomatik_alarm": False,
                "otomatik_saha_gorevi": False,
                "15_eylul_otomatik_terfi": False,
                "bagimsiz_santiye_dogrulanmadi": True,
                "durum": CURRENT_STATUS,
                "guncel_guclu": True,
                "tekrar_temporal_destek": False,
                "farkli_sentinel_sahnesi_gorulme_sayisi": 1,
                "sentinel_sahne_gecmisi": [scene_item],
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
                "temporal_sinif": str(candidate.get("temporal_sinif") or ""),
                "ani_baslangic_destegi": bool(candidate.get("ani_baslangic_destegi")),
                "devam_eden_hareket_destegi": bool(
                    candidate.get("devam_eden_hareket_destegi")
                ),
                "ani_baslangic_orani": candidate.get("ani_baslangic_orani"),
                "uc_sahne_gecerli_oran": candidate.get("uc_sahne_gecerli_oran"),
                "en_yakin_250plus_m": candidate.get("en_yakin_250plus_m"),
                "karar_nedeni": (
                    "Ana 250 m²+ adaya yakın olduğu için bağımsız şantiye sayılmayan; "
                    "ancak güçlü spektral/kompakt + temporal kanıtı bulunan 150-249 m² "
                    "parça kalıcı diagnostik hafızada tutuluyor."
                ),
            }
            entries.append(entry)
            used.add(len(entries) - 1)
            continue

        used.add(match)
        entry = entries[match]
        history = _history(entry)
        if scene_item not in history:
            history.append(scene_item)
        history = history[-MAX_SCENE_HISTORY:]
        entry.update(
            {
                "mikro_kaynak": SOURCE,
                "alarm": False,
                "saha_gorevi": False,
                "otomatik_alarm": False,
                "otomatik_saha_gorevi": False,
                "15_eylul_otomatik_terfi": False,
                "bagimsiz_santiye_dogrulanmadi": True,
                "durum": CURRENT_STATUS,
                "guncel_guclu": True,
                "tekrar_temporal_destek": len(history) >= 2,
                "farkli_sentinel_sahnesi_gorulme_sayisi": len(history),
                "sentinel_sahne_gecmisi": history,
                "son_guclu_gorulme_tarihi": scene_date,
                "son_guclu_sentinel_item": scene_item,
                "yaklasik_mevki": str(
                    candidate.get("yaklasik_mevki")
                    or entry.get("yaklasik_mevki")
                    or "Mevki doğrulanmadı"
                ),
                "enlem": round(float(candidate["enlem"]), 6),
                "boylam": round(float(candidate["boylam"]), 6),
                "alan_m2": int(candidate["alan_m2"]),
                "harita": str(candidate.get("harita") or entry.get("harita") or ""),
                "temporal_sinif": str(
                    candidate.get("temporal_sinif") or entry.get("temporal_sinif") or ""
                ),
                "ani_baslangic_destegi": bool(candidate.get("ani_baslangic_destegi")),
                "devam_eden_hareket_destegi": bool(
                    candidate.get("devam_eden_hareket_destegi")
                ),
                "ani_baslangic_orani": candidate.get(
                    "ani_baslangic_orani", entry.get("ani_baslangic_orani")
                ),
                "uc_sahne_gecerli_oran": candidate.get(
                    "uc_sahne_gecerli_oran", entry.get("uc_sahne_gecerli_oran")
                ),
                "en_yakin_250plus_m": candidate.get(
                    "en_yakin_250plus_m", entry.get("en_yakin_250plus_m")
                ),
                "son_esleme_mesafe_m": round(float(distance or 0.0), 1),
            }
        )
        entry.setdefault("ilk_gorulme_tarihi", scene_date)

    entries.sort(
        key=lambda item: (
            0 if item.get("guncel_guclu") else 1,
            0 if item.get("tekrar_temporal_destek") else 1,
            str(item.get("bolge") or ""),
            str(item.get("mikro_iz_id") or ""),
        )
    )

    current_count = sum(bool(item.get("guncel_guclu")) for item in entries)
    repeat_count = sum(bool(item.get("tekrar_temporal_destek")) for item in entries)
    background_count = len(entries) - current_count

    return {
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "alarm": False,
        "saha_gorevi": False,
        "otomatik_alarm": False,
        "otomatik_saha_gorevi": False,
        "15_eylul_otomatik_terfi": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": [MICRO_MIN_M2, MICRO_MAX_M2],
        "esleme_mesafesi_m": MATCH_DISTANCE_M,
        "minimum_uc_sahne_gecerli_oran": MIN_VALID_RATIO,
        "amac": (
            "250 m²+ ana adaya yakınlık nedeniyle normal MİKRO kısa listeden bastırılan "
            "ama güçlü temporal kanıt alan 150-249 m² parçaların Sentinel sahneleri "
            "arasında kaybolmasını önlemek."
        ),
        "uyari": (
            "Bu hafıza bağımsız şantiye kanıtı değildir; alarm/görev üretmez ve "
            "15 Eylül'de otomatik terfi yapmaz. Yeni sahnede bağımsız 250 m²+ ana "
            "aday oluşursa ancak ayrı bir güvenli öncelik katmanı tarafından kanıt "
            "olarak değerlendirilebilir."
        ),
        "guncel_guclu": current_count,
        "arka_plan_takip": background_count,
        "tekrar_temporal_destek": repeat_count,
        "adaylar": entries,
    }


def _semantic(payload: dict) -> dict:
    result = dict(payload)
    result.pop("olusturma", None)
    return result


def update_watchlist() -> tuple[dict, bool]:
    probe = _load_json(PROBE_FILE)
    if not probe:
        raise SystemExit("micro_proximity_temporal_probe.json bulunamadı.")
    previous = _load_json(OUTPUT_FILE)
    payload = build_watchlist(probe, previous)

    if previous and _semantic(previous) == _semantic(payload):
        print("Yakın-ana temporal hafızada anlamlı değişiklik yok.")
        return previous, False

    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Yakın-ana temporal hafıza güncellendi: "
        f"{payload['guncel_guclu']} güncel güçlü, "
        f"{payload['tekrar_temporal_destek']} farklı-sahne tekrar, "
        f"{payload['arka_plan_takip']} arka plan."
    )
    return payload, True


def _probe(scene_item="SCENE_A", scene_date="05.09.2026", *, supported=True):
    return {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "operasyonel_kisa_listeye_etki": False,
        "bolgeler": {
            "uzunkuyu": {
                "son_item": scene_item,
                "son_tarih": scene_date,
                "adaylar": [
                    {
                        "alarm": False,
                        "saha_gorevi": False,
                        "bolge": "uzunkuyu",
                        "yaklasik_mevki": "Gülbahçe",
                        "enlem": 38.3328,
                        "boylam": 26.6456,
                        "alan_m2": 200,
                        "temporal_probe_only": True,
                        "operasyonel_kisa_liste": False,
                        "spektral_kisa_liste_kapisi": True,
                        "ana_adaya_yakin": True,
                        "genis_hareket_kumesi_riski": False,
                        "onceki_zemin_hareketli_riski": False,
                        "uc_sahne_gecerli_oran": 1.0,
                        "ani_baslangic_destegi": supported,
                        "devam_eden_hareket_destegi": False,
                        "ani_baslangic_orani": 8.0,
                        "temporal_sinif": "ANI_BASLANGIC_DESTEGI",
                        "en_yakin_250plus_m": 18,
                    }
                ],
            }
        },
    }


def _self_check():
    first = build_watchlist(_probe(), {})
    assert first["ana_uretim_esigi_m2"] == 250
    assert first["mikro_aralik_m2"] == [150, 249]
    assert first["guncel_guclu"] == 1
    assert first["tekrar_temporal_destek"] == 0
    row = first["adaylar"][0]
    assert row["yaklasik_mevki"] == "Gülbahçe"
    assert row["alan_m2"] == 200
    assert row["alarm"] is False and row["saha_gorevi"] is False
    assert row["15_eylul_otomatik_terfi"] is False

    same = build_watchlist(_probe(), first)
    assert same["adaylar"][0]["farkli_sentinel_sahnesi_gorulme_sayisi"] == 1
    assert same["tekrar_temporal_destek"] == 0

    moved_probe = _probe("SCENE_B", "10.09.2026")
    moved_probe["bolgeler"]["uzunkuyu"]["adaylar"][0]["enlem"] = 38.3329
    second = build_watchlist(moved_probe, first)
    assert len(second["adaylar"]) == 1
    assert second["tekrar_temporal_destek"] == 1
    assert second["adaylar"][0]["farkli_sentinel_sahnesi_gorulme_sayisi"] == 2

    empty = _probe("SCENE_C", "15.09.2026")
    empty["bolgeler"]["uzunkuyu"]["adaylar"] = []
    background = build_watchlist(empty, second)
    assert background["guncel_guclu"] == 0
    assert background["arka_plan_takip"] == 1
    assert background["adaylar"][0]["durum"] == BACKGROUND_STATUS
    assert background["adaylar"][0]["alarm"] is False
    assert background["adaylar"][0]["saha_gorevi"] is False

    weak = build_watchlist(_probe(supported=False), {})
    assert weak["adaylar"] == []

    invalid = _probe()
    invalid["bolgeler"]["uzunkuyu"]["adaylar"][0]["alan_m2"] = 250
    assert build_watchlist(invalid, {})["adaylar"] == []

    broad = _probe()
    broad["bolgeler"]["uzunkuyu"]["adaylar"][0]["genis_hareket_kumesi_riski"] = True
    assert build_watchlist(broad, {})["adaylar"] == []

    moving_ground = _probe()
    moving_ground["bolgeler"]["uzunkuyu"]["adaylar"][0]["onceki_zemin_hareketli_riski"] = True
    assert build_watchlist(moving_ground, {})["adaylar"] == []

    print(
        "Yakın-ana MİKRO temporal hafıza öz testi başarılı: "
        "yalnız güçlü 150-249 m² diagnostik kanıt kalıcı tutuluyor; alarm/görev yok."
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if args.check_only:
        return
    update_watchlist()


if __name__ == "__main__":
    main()
