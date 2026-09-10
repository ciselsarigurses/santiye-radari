"""Güçlü MİKRO izleme kaydına aynı bileşenin sinyal çekirdeğini güvenle ekler.

Bu köprü 150-249 m² MİKRO katmanını alarma veya saha görevine yükseltmez. Mevcut
izleme koordinatı bağlı bileşenin temsilci noktası olarak korunur; yalnız aynı
Sentinel sahnesiyle eşleşen ve en fazla 15 m içeride kalan en güçlü çoklu-spektral
piksel ikinci bir yeniden-kontrol koordinatı olarak saklanır.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


BASE = Path(__file__).resolve().parent
WATCHLIST_FILE = BASE / "micro_site_watchlist.json"
COORDINATE_FILE = BASE / "micro_site_coordinate_audit.json"

MAIN_THRESHOLD_M2 = 250
MICRO_INTERVAL = [150, 249]
MAX_CORE_SHIFT_M = 15.0
SAFE_QUALITIES = {"AYNI_PIKSEL", "KOMSULUK_ICI_10M"}


def _key(item):
    try:
        return (
            str(item.get("bolge") or ""),
            round(float(item.get("enlem")), 6),
            round(float(item.get("boylam")), 6),
            int(round(float(item.get("alan_m2")))),
        )
    except (TypeError, ValueError):
        return None


def _coordinate_index(payload):
    index = {}
    for row in payload.get("adaylar") or []:
        if not isinstance(row, dict):
            continue
        key = _key(row)
        if key is None:
            continue
        quality = str(row.get("koordinat_kalitesi") or "")
        try:
            shift = float(row.get("sinyal_cekirdegi_sapma_m"))
            core_lat = float(row.get("sinyal_cekirdegi_enlem"))
            core_lon = float(row.get("sinyal_cekirdegi_boylam"))
        except (TypeError, ValueError):
            continue
        if quality not in SAFE_QUALITIES or shift > MAX_CORE_SHIFT_M:
            continue
        if row.get("alarm") is True or row.get("saha_gorevi") is True:
            continue
        latest_item = str(row.get("latest_item") or "").strip()
        if not latest_item:
            continue
        clean = dict(row)
        clean["sinyal_cekirdegi_enlem"] = round(core_lat, 6)
        clean["sinyal_cekirdegi_boylam"] = round(core_lon, 6)
        index[key] = clean
    return index


def bridge(watchlist, coordinate):
    if int(watchlist.get("ana_uretim_esigi_m2") or 0) != MAIN_THRESHOLD_M2:
        raise AssertionError("MİKRO watchlist ana üretim eşiği 250 m² değil.")
    if list(watchlist.get("mikro_aralik_m2") or []) != MICRO_INTERVAL:
        raise AssertionError("MİKRO watchlist bandı 150-249 m² değil.")
    if int(coordinate.get("ana_uretim_esigi_m2") or 0) != MAIN_THRESHOLD_M2:
        raise AssertionError("Koordinat audit ana üretim eşiği 250 m² değil.")
    if list(coordinate.get("mikro_aralik_m2") or []) != MICRO_INTERVAL:
        raise AssertionError("Koordinat audit bandı 150-249 m² değil.")

    index = _coordinate_index(coordinate)
    matched = 0
    for entry in watchlist.get("adaylar") or []:
        if not isinstance(entry, dict) or entry.get("guncel_guclu") is not True:
            continue
        row = index.get(_key(entry))
        if row is None:
            continue
        if str(entry.get("son_guclu_sentinel_item") or "") != str(row.get("latest_item") or ""):
            continue

        # Temsilci koordinatı ve ana harita kesinlikle değişmez. Sinyal çekirdeği
        # yalnız aynı küçük bağlı bileşendeki ikinci diagnostik hedeftir.
        entry.update(
            {
                "sinyal_cekirdegi_enlem": row["sinyal_cekirdegi_enlem"],
                "sinyal_cekirdegi_boylam": row["sinyal_cekirdegi_boylam"],
                "sinyal_cekirdegi_sapma_m": row.get("sinyal_cekirdegi_sapma_m"),
                "koordinat_kalitesi": row.get("koordinat_kalitesi"),
                "sinyal_cekirdegi_harita": row.get("cekirdek_harita"),
                "sinyal_cekirdegi_kaynagi": "AYNI_BILESEN_EN_GUCLU_COKLU_SPEKTRAL_PIKSEL",
                "sinyal_cekirdegi_yalniz_diagnostik": True,
            }
        )
        entry["alarm"] = False
        entry["saha_gorevi"] = False
        matched += 1

    watchlist["koordinat_cekirdegi_eslesen_guncel_guclu"] = matched
    watchlist["koordinat_cekirdegi_alarm_etkisi"] = False
    watchlist["koordinat_cekirdegi_saha_gorevi_etkisi"] = False
    watchlist["koordinat_cekirdegi_kurali"] = (
        "Yalnız aynı koordinat+alan ve aynı Sentinel sahnesiyle eşleşen güncel güçlü "
        "MİKRO izine <=15 m aynı-bileşen sinyal çekirdeği eklenir. Temsilci koordinatı "
        "değişmez; çekirdek ikinci diagnostik yeniden-kontrol hedefidir."
    )
    return watchlist


def _semantic(payload):
    clean = dict(payload)
    clean.pop("olusturma", None)
    return clean


def update_watchlist():
    if not WATCHLIST_FILE.exists() or not COORDINATE_FILE.exists():
        raise RuntimeError("MİKRO watchlist veya koordinat audit dosyası bulunamadı.")
    watchlist = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    coordinate = json.loads(COORDINATE_FILE.read_text(encoding="utf-8"))
    before = _semantic(json.loads(json.dumps(watchlist, ensure_ascii=False)))
    result = bridge(watchlist, coordinate)
    if before == _semantic(result):
        print("MİKRO watchlist sinyal çekirdeği eşleşmesinde anlamlı değişiklik yok.")
        return False
    WATCHLIST_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "MİKRO watchlist sinyal çekirdeği güncellendi: "
        f"eşleşen={result['koordinat_cekirdegi_eslesen_guncel_guclu']}. "
        "Alarm/görev ve 250 m² ana eşik değişmedi."
    )
    return True


def _self_check():
    watchlist = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "adaylar": [
            {
                "bolge": "cesme",
                "enlem": 38.287679,
                "boylam": 26.266305,
                "alan_m2": 200,
                "guncel_guclu": True,
                "son_guclu_sentinel_item": "S2_CURRENT",
                "harita": "representative-map",
                "alarm": False,
                "saha_gorevi": False,
            },
            {
                "bolge": "cesme",
                "enlem": 38.250000,
                "boylam": 26.320000,
                "alan_m2": 200,
                "guncel_guclu": False,
                "son_guclu_sentinel_item": "S2_OLD",
                "alarm": False,
                "saha_gorevi": False,
            },
        ],
    }
    coordinate = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "adaylar": [
            {
                "bolge": "cesme",
                "enlem": 38.287679,
                "boylam": 26.266305,
                "alan_m2": 200,
                "sinyal_cekirdegi_enlem": 38.287679,
                "sinyal_cekirdegi_boylam": 26.266420,
                "sinyal_cekirdegi_sapma_m": 10.0,
                "koordinat_kalitesi": "KOMSULUK_ICI_10M",
                "cekirdek_harita": "core-map",
                "latest_item": "S2_CURRENT",
                "alarm": False,
                "saha_gorevi": False,
            }
        ],
    }
    result = bridge(watchlist, coordinate)
    current = result["adaylar"][0]
    background = result["adaylar"][1]
    assert current["enlem"] == 38.287679 and current["boylam"] == 26.266305
    assert current["harita"] == "representative-map"
    assert current["sinyal_cekirdegi_boylam"] == 26.26642
    assert current["sinyal_cekirdegi_yalniz_diagnostik"] is True
    assert current["alarm"] is False and current["saha_gorevi"] is False
    assert "sinyal_cekirdegi_enlem" not in background
    assert result["koordinat_cekirdegi_eslesen_guncel_guclu"] == 1
    assert result["koordinat_cekirdegi_alarm_etkisi"] is False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("MİKRO koordinat-watchlist köprüsü öz testi başarılı.")
        return
    update_watchlist()


if __name__ == "__main__":
    main()
