"""Taze Sentinel görevleri için güvenli ikinci navigasyon hedefi üretir.

Bu modül üretim aday koordinatını, görev kimliğini, alarm eşiğini veya saha durumunu
değiştirmez. ``coordinate_precision_audit.json`` içindeki aynı bağlı bileşenden
seçilmiş BSI×RGB sinyal-ağırlıklı piksel yalnızca şu koşullarda ikinci bir
"sinyal çekirdeği" hedefi olarak gösterilebilir:

* görev yeni Sentinel görüntüsünden geliyorsa,
* görev 250 m²+ ana üretim yolundaysa,
* audit aynı Sentinel son sahnesine aitse,
* audit adayı mevcut görev koordinatına yakın ve alanı aynıysa,
* önerilen kayma en fazla 20 m ise.

Amaç saha ekibine 10 m sınıfı Sentinel çözünürlüğünde daha odaklı bir kontrol
noktası vermek; özgün koordinatı kesin adres/parsel gibi değiştirmek değildir.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime
from pathlib import Path


AUDIT_FILE = Path(__file__).with_name("coordinate_precision_audit.json")
MAIN_MIN_AREA_M2 = 250
MATCH_RADIUS_M = 20.0
MAX_SIGNAL_SHIFT_M = 20.0


def _number(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _distance_m(lat1, lon1, lat2, lon2):
    mean_lat = math.radians((lat1 + lat2) / 2)
    north_m = (lat2 - lat1) * 110570
    east_m = (lon2 - lon1) * 111320 * math.cos(mean_lat)
    return float(math.hypot(north_m, east_m))


def _normalize_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _item_date(item_id):
    match = re.search(r"_(20\d{6})T", str(item_id or ""))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def load_audit(path=AUDIT_FILE):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def signal_core_target(item, audit_payload):
    """Güvenli ise ikinci navigasyon hedefini döndür; aksi halde ``None``."""
    if not isinstance(item, dict) or not isinstance(audit_payload, dict):
        return None
    if item.get("yeni_goruntu") is not True:
        return None

    area = _number(item.get("alan_m2"))
    latitude = _number(item.get("enlem"))
    longitude = _number(item.get("boylam"))
    if area is None or area < MAIN_MIN_AREA_M2 or latitude is None or longitude is None:
        return None

    scene_date = _normalize_date(item.get("son_tarih"))
    if scene_date is None:
        return None

    matches = []
    for region_key, region in (audit_payload.get("bolgeler") or {}).items():
        if not isinstance(region, dict) or region.get("durum") != "ok":
            continue
        audit_scene_date = _item_date(region.get("son_item"))
        if audit_scene_date != scene_date:
            continue
        for row in region.get("adaylar") or []:
            if not isinstance(row, dict):
                continue
            row_area = _number(row.get("alan_m2"))
            raw_lat = _number(row.get("mevcut_enlem"))
            raw_lon = _number(row.get("mevcut_boylam"))
            signal_lat = _number(row.get("sinyal_agirlikli_enlem"))
            signal_lon = _number(row.get("sinyal_agirlikli_boylam"))
            shift = _number(row.get("geometrik_sinyal_kaymasi_m"))
            if None in (row_area, raw_lat, raw_lon, signal_lat, signal_lon, shift):
                continue
            # Sentinel alanları piksel katlarıdır; aynı bağlı bileşeni yanlış
            # eşlememek için alanın da aynı olmasını şart koş.
            if abs(row_area - area) > 1.0:
                continue
            match_distance = _distance_m(latitude, longitude, raw_lat, raw_lon)
            if match_distance > MATCH_RADIUS_M or shift > MAX_SIGNAL_SHIFT_M:
                continue
            matches.append(
                (
                    match_distance,
                    {
                        "enlem": round(signal_lat, 6),
                        "boylam": round(signal_lon, 6),
                        "kayma_m": round(shift, 1),
                        "esleme_mesafe_m": round(match_distance, 1),
                        "bolge": str(region_key),
                        "son_tarih": scene_date,
                        "harita": (
                            "https://www.google.com/maps/dir/?api=1&destination="
                            f"{signal_lat:.6f},{signal_lon:.6f}"
                        ),
                    },
                )
            )

    if not matches:
        return None
    matches.sort(key=lambda pair: (pair[0], pair[1]["kayma_m"]))
    best = matches[0][1]
    # Aynı piksel seçildiyse ikinci buton gereksizdir.
    if best["kayma_m"] < 5.0:
        return None
    return best


def _self_check():
    audit = {
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "son_item": "S2B_T35SMC_20260915T090620_L2A",
                "adaylar": [
                    {
                        "alan_m2": 400,
                        "mevcut_enlem": 38.300000,
                        "mevcut_boylam": 26.300000,
                        "sinyal_agirlikli_enlem": 38.300090,
                        "sinyal_agirlikli_boylam": 26.300000,
                        "geometrik_sinyal_kaymasi_m": 10.0,
                    }
                ],
            }
        }
    }
    fresh = {
        "yeni_goruntu": True,
        "alan_m2": 400,
        "enlem": 38.300000,
        "boylam": 26.300000,
        "son_tarih": "15.09.2026",
    }
    result = signal_core_target(fresh, audit)
    assert result and result["kayma_m"] == 10.0
    assert result["enlem"] == 38.30009

    stale = dict(fresh, yeni_goruntu=False)
    assert signal_core_target(stale, audit) is None

    micro = dict(fresh, alan_m2=200)
    assert signal_core_target(micro, audit) is None

    wrong_scene = dict(fresh, son_tarih="16.09.2026")
    assert signal_core_target(wrong_scene, audit) is None

    far = dict(fresh, enlem=38.301000)
    assert signal_core_target(far, audit) is None

    too_far_shift = json.loads(json.dumps(audit))
    too_far_shift["bolgeler"]["cesme"]["adaylar"][0]["geometrik_sinyal_kaymasi_m"] = 30.0
    assert signal_core_target(fresh, too_far_shift) is None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print(
            "Sinyal çekirdeği navigasyon öz testi başarılı: yalnız taze 250 m²+ "
            "aynı-sahne/eş-bileşen adayına ikinci hedef veriliyor."
        )
        return
    print("coordinate_navigation yalnız uygulama yardımcı modülüdür; üretim verisini değiştirmez.")


if __name__ == "__main__":
    main()
