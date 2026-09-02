"""Güncel üretim kıyı filtresine düşen eski açık uydu görevlerini ölçer.

Bu denetim görev kapatmaz veya rota sırasını değiştirmez. Yalnız, sahada doğrulanmış
bir kıyı yanlış pozitifinin güncel 30 m SCL-su tamponu tarafından artık elendiği
bölgelerde, son analizde tekrar görünmeyip tarihsel metadatasıyla açık kalan başka
görevlerin de aynı güncel sert filtreye düşüp düşmediğini raporlar.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np

import satellite
from daily_report import ISTANBUL


BASE_DIR = Path(__file__).resolve().parent
COASTAL_AUDIT = BASE_DIR / "coastal_false_positive_audit.json"
LATEST_REPORT = BASE_DIR / "latest_report.json"
OUTPUT_FILE = BASE_DIR / "stale_coastal_task_audit.json"
ACTIVE_STATUSES = {"KONTROLE_GIT", "TEKRAR_GIT"}


def _load_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _verified_current_coastal_regions(audit):
    regions = set()
    for region_key, data in (audit.get("bolgeler") or {}).items():
        if not isinstance(data, dict):
            continue
        for point in data.get("yanlis_pozitifler") or []:
            if not isinstance(point, dict):
                continue
            baseline = (point.get("yaricap_sonuclari") or {}).get(
                str(satellite.COASTAL_WATER_BUFFER_PIXELS), {}
            )
            if bool(baseline.get("su_tamponunda")):
                regions.add(str(region_key))
                break
    return regions


def _stale_candidates(report, region_label):
    values = []
    for item in report.get("saha_adaylari") or []:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("gorev_id") or "")
        if not task_id.startswith("U"):
            continue
        if str(item.get("bolge") or "") != region_label:
            continue
        status = str(item.get("saha_durumu") or "KONTROLE_GIT").upper()
        if status not in ACTIVE_STATUSES:
            continue
        reason = str(item.get("oncelik_nedeni") or "").casefold()
        note = str(item.get("konum_notu") or "").casefold()
        if "son analiz kümesinde tekrar görünmedi" not in reason and (
            "son yeniden analizde küme tekrar görünmedi" not in note
        ):
            continue
        try:
            latitude = float(item.get("enlem"))
            longitude = float(item.get("boylam"))
        except (TypeError, ValueError):
            continue
        values.append({**item, "enlem": latitude, "boylam": longitude})
    return values


def _water_buffer(region_key):
    bbox = satellite.REGIONS[region_key]["bbox"]
    older, latest = satellite.sentinel_pair(region_key)
    height, width = satellite._output_shape(bbox)
    older_scl = satellite._read_asset(
        older, "scl", bbox, height, width, "nearest"
    )[0]
    latest_scl = satellite._read_asset(
        latest, "scl", bbox, height, width, "nearest"
    )[0]
    water = (older_scl == 6) | (latest_scl == 6)
    return bbox, satellite._dilate_mask(water, satellite.COASTAL_WATER_BUFFER_PIXELS)


def _in_mask(latitude, longitude, bbox, mask):
    west, south, east, north = bbox
    height, width = mask.shape
    row = int((north - latitude) / (north - south) * height)
    column = int((longitude - west) / (east - west) * width)
    if not (0 <= row < height and 0 <= column < width):
        return False
    return bool(mask[row, column])


def build_audit():
    coastal = _load_json(COASTAL_AUDIT)
    report = _load_json(LATEST_REPORT)
    verified_regions = _verified_current_coastal_regions(coastal)
    results = {}
    total = 0

    for region_key in sorted(verified_regions):
        region = satellite.REGIONS.get(region_key)
        if not region:
            continue
        candidates = _stale_candidates(report, str(region["label"]))
        bbox, mask = _water_buffer(region_key)
        flagged = []
        for item in candidates:
            if not _in_mask(item["enlem"], item["boylam"], bbox, mask):
                continue
            flagged.append(
                {
                    "gorev_id": item.get("gorev_id"),
                    "mahalle": item.get("mahalle"),
                    "enlem": round(float(item["enlem"]), 6),
                    "boylam": round(float(item["boylam"]), 6),
                    "alan_m2": item.get("alan_m2"),
                    "oncelik": item.get("oncelik"),
                    "bekleme_gun": item.get("bekleme_gun"),
                    "tarihsel_esleme_mesafe_m": item.get("tarihsel_esleme_mesafe_m"),
                }
            )
        total += len(flagged)
        results[region_key] = {
            "bolge": region["label"],
            "tarihsel_acik_gorev": len(candidates),
            "guncel_kiyi_tamponunda": len(flagged),
            "gorevler": flagged,
        }

    return {
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "amac": (
            "Sahada doğrulanmış kıyı yanlış pozitif mekanizmasının görüldüğü bölgelerde, "
            "güncel 30 m sert kıyı filtresine düşen tarihsel açık görevleri bulmak; "
            "bu dosya görev veya rota değiştirmez."
        ),
        "uretim_degistirildi": False,
        "dogrulanmis_kiyi_mekanizmali_bolge": sorted(verified_regions),
        "guncel_kiyi_tamponundaki_eski_acik_gorev_toplam": total,
        "bolgeler": results,
    }


def _self_check():
    synthetic = {
        "bolgeler": {
            "cesme": {
                "yanlis_pozitifler": [
                    {
                        "yaricap_sonuclari": {
                            str(satellite.COASTAL_WATER_BUFFER_PIXELS): {
                                "su_tamponunda": True
                            }
                        }
                    }
                ]
            }
        }
    }
    assert _verified_current_coastal_regions(synthetic) == {"cesme"}


def main():
    _self_check()
    payload = build_audit()
    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
