"""Pozitif temel/kepçe recall adaylarına baseline tarla/bahçe negatif bağlamını uygular.

Bu katman yalnız diagnostiktir; alarm, saha görevi veya operasyonel rota üretmez.
Amaç, pozitif-evidence katmanında güçlü görünen adayların 13 Eylül sakin baseline
→ en güncel post-onset karşılaştırmasında korunmuş/karma bitki dokusu taşıyorsa
sessizce güçlü aday sayılmasını önlemektir.

Negatif bağlam yalnız aynı post-onset Sentinel-2 sahne tarihi için geçerlidir.
Daha yeni bir Sentinel sahnesi geldiğinde eski negatif bağlam otomatik veto olarak
kullanılmaz; aday yeni kanıtla yeniden değerlendirilir.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


BASE = Path(__file__).resolve().parent
POSITIVE_JSON = BASE / "foundation_excavation_positive_evidence_review.json"
VEGETATION_JSON = BASE / "post_onset_baseline_vegetation_guard_review.json"
OUTPUT_JSON = BASE / "foundation_excavation_positive_context_review.json"

MATCH_RADIUS_M = 30.0
NEGATIVE_REASON = "BASELINE_BITKI_TARLA_NEGATIF_BAGLAM"


def _load(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _point(item):
    try:
        return float(item["enlem"]), float(item["boylam"])
    except (KeyError, TypeError, ValueError):
        return None


def _distance_m(a, b):
    lat1, lon1 = float(a[0]), float(a[1])
    lat2, lon2 = float(b[0]), float(b[1])
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _nearest(target, records):
    target_point = _point(target)
    if target_point is None:
        return None, None
    nearest = None
    nearest_distance = None
    for record in records:
        record_point = _point(record)
        if record_point is None:
            continue
        distance = _distance_m(target_point, record_point)
        if nearest_distance is None or distance < nearest_distance:
            nearest = record
            nearest_distance = distance
    return nearest, nearest_distance


def _suppressed_records(region):
    return [
        item
        for item in (region.get("bastirilan_adaylar") or [])
        if isinstance(item, dict)
        and item.get("kalici_bitki_negatif_baglami") is True
        and _point(item) is not None
    ]


def _append_reason(existing, reason):
    reasons = [str(x) for x in (existing or []) if str(x).strip()]
    if reason not in reasons:
        reasons.append(reason)
    return reasons


def _guard_region(region_key, positive_region, vegetation_region):
    positive_scene = str(positive_region.get("s2_son_tarih") or "").strip()
    vegetation_scene = str(vegetation_region.get("post_onset_tarih") or "").strip()
    scene_match = bool(
        positive_scene
        and vegetation_scene
        and positive_scene == vegetation_scene
    )

    negatives = _suppressed_records(vegetation_region) if scene_match else []
    rows = []
    for candidate in positive_region.get("adaylar") or []:
        if not isinstance(candidate, dict) or _point(candidate) is None:
            continue
        row = dict(candidate)
        nearest, distance = _nearest(candidate, negatives)
        matched = bool(
            scene_match
            and nearest is not None
            and distance is not None
            and distance <= MATCH_RADIUS_M
        )

        original_main = candidate.get("ana_esik_pozitif_destekli_diagnostik") is True
        original_micro = candidate.get("mikro_pozitif_destekli_diagnostik") is True
        row["baseline_bitki_negatif_baglami"] = matched
        row["baseline_bitki_negatif_mesafe_m"] = (
            round(distance, 1) if matched and distance is not None else None
        )
        row["baseline_bitki_negatif_neden"] = (
            nearest.get("negatif_baglamsal_neden") if matched and nearest else None
        )
        row["baseline_negatif_sahne_eslesiyor"] = scene_match
        if matched:
            row["negatif_baglamsal_engel"] = True
            row["negatif_baglamsal_nedenler"] = _append_reason(
                candidate.get("negatif_baglamsal_nedenler"), NEGATIVE_REASON
            )
        row["ana_esik_pozitif_destekli_diagnostik_korumali"] = bool(
            original_main and not matched
        )
        row["mikro_pozitif_destekli_diagnostik_korumali"] = bool(
            original_micro and not matched
        )
        row["alarm"] = False
        row["saha_gorevi"] = False
        rows.append(row)

    rows.sort(
        key=lambda item: (
            bool(item.get("ana_esik_pozitif_destekli_diagnostik_korumali")),
            bool(item.get("mikro_pozitif_destekli_diagnostik_korumali")),
            bool(item.get("coklu_pozitif_kanit")),
            float(item.get("morfoloji_puani") or 0),
        ),
        reverse=True,
    )
    return {
        "bolge": positive_region.get("bolge") or region_key,
        "pozitif_s2_son_tarih": positive_scene or None,
        "baseline_post_onset_tarih": vegetation_scene or None,
        "sahne_tarihi_eslesiyor": scene_match,
        "baseline_negatif_aday_sayisi": len(negatives),
        "girdi_aday_sayisi": len(rows),
        "baseline_negatif_eslesen_sayi": sum(
            1 for item in rows if item["baseline_bitki_negatif_baglami"]
        ),
        "ana_esik_once": sum(
            1 for item in rows if item.get("ana_esik_pozitif_destekli_diagnostik") is True
        ),
        "ana_esik_sonra": sum(
            1 for item in rows
            if item.get("ana_esik_pozitif_destekli_diagnostik_korumali") is True
        ),
        "mikro_once": sum(
            1 for item in rows if item.get("mikro_pozitif_destekli_diagnostik") is True
        ),
        "mikro_sonra": sum(
            1 for item in rows
            if item.get("mikro_pozitif_destekli_diagnostik_korumali") is True
        ),
        "adaylar": rows,
    }


def audit(positive=None, vegetation=None):
    positive = positive if isinstance(positive, dict) else _load(POSITIVE_JSON)
    vegetation = vegetation if isinstance(vegetation, dict) else _load(VEGETATION_JSON)

    regions = {}
    for region_key in ("cesme", "uzunkuyu"):
        regions[region_key] = _guard_region(
            region_key,
            (positive.get("bolgeler") or {}).get(region_key) or {},
            (vegetation.get("bolgeler") or {}).get(region_key) or {},
        )

    total_matches = sum(x["baseline_negatif_eslesen_sayi"] for x in regions.values())
    total_main_before = sum(x["ana_esik_once"] for x in regions.values())
    total_main_after = sum(x["ana_esik_sonra"] for x in regions.values())
    total_micro_before = sum(x["mikro_once"] for x in regions.values())
    total_micro_after = sum(x["mikro_sonra"] for x in regions.values())

    return {
        "surum": 1,
        "amac": (
            "Pozitif temel/kepçe recall adaylarını aynı-sahne baseline bitki/tarla "
            "negatif bağlamıyla ikinci kez korumak"
        ),
        "gercek_derinlik_olcumu": False,
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "baseline_negatif_eslesme_yaricapi_m": MATCH_RADIUS_M,
        "uretim_filtresi": False,
        "alarm": False,
        "saha_gorevi": False,
        "rota_kapisi_hazir": False,
        "toplam_baseline_negatif_eslesen": total_matches,
        "toplam_ana_esik_once": total_main_before,
        "toplam_ana_esik_sonra": total_main_after,
        "toplam_mikro_once": total_micro_before,
        "toplam_mikro_sonra": total_micro_after,
        "bolgeler": regions,
        "not": (
            "Bu çıktı yalnız diagnostiktir. Baseline tarla/bahçe negatif bağlamı ancak "
            "pozitif-evidence S2 son tarihi ile vegetation guard post-onset tarihi aynıysa "
            "ve aday 30 m içinde eşleşiyorsa uygulanır. Böylece daha yeni Sentinel sahnesi "
            "geldiğinde eski negatif bağlam kalıcı veto olmaz. 250 m² ana eşik ve 150–249 m² "
            "MİKRO politikası değişmez; alarm veya saha görevi üretilmez."
        ),
    }


def _self_check():
    positive = {
        "bolgeler": {
            "cesme": {
                "s2_son_tarih": "18.09.2026",
                "adaylar": [
                    {
                        "enlem": 38.30,
                        "boylam": 26.30,
                        "spektral_etki_alani_m2": 400,
                        "morfoloji_puani": 95,
                        "coklu_pozitif_kanit": True,
                        "ana_esik_pozitif_destekli_diagnostik": True,
                        "mikro_pozitif_destekli_diagnostik": False,
                        "negatif_baglamsal_nedenler": [],
                    },
                    {
                        "enlem": 38.31,
                        "boylam": 26.31,
                        "spektral_etki_alani_m2": 200,
                        "morfoloji_puani": 90,
                        "coklu_pozitif_kanit": True,
                        "ana_esik_pozitif_destekli_diagnostik": False,
                        "mikro_pozitif_destekli_diagnostik": True,
                        "negatif_baglamsal_nedenler": [],
                    },
                ],
            },
            "uzunkuyu": {"s2_son_tarih": "18.09.2026", "adaylar": []},
        }
    }
    vegetation = {
        "bolgeler": {
            "cesme": {
                "post_onset_tarih": "18.09.2026",
                "bastirilan_adaylar": [
                    {
                        "enlem": 38.30001,
                        "boylam": 26.30001,
                        "kalici_bitki_negatif_baglami": True,
                        "negatif_baglamsal_neden": "karma tarla/bahçe bağlamı",
                    }
                ],
            },
            "uzunkuyu": {"post_onset_tarih": "18.09.2026", "bastirilan_adaylar": []},
        }
    }
    payload = audit(positive, vegetation)
    rows = payload["bolgeler"]["cesme"]["adaylar"]
    by_lat = {round(float(x["enlem"]), 2): x for x in rows}
    assert by_lat[38.30]["baseline_bitki_negatif_baglami"] is True
    assert by_lat[38.30]["ana_esik_pozitif_destekli_diagnostik_korumali"] is False
    assert NEGATIVE_REASON in by_lat[38.30]["negatif_baglamsal_nedenler"]
    assert by_lat[38.31]["baseline_bitki_negatif_baglami"] is False
    assert by_lat[38.31]["mikro_pozitif_destekli_diagnostik_korumali"] is True
    assert payload["toplam_ana_esik_once"] == 1
    assert payload["toplam_ana_esik_sonra"] == 0
    assert payload["toplam_mikro_once"] == 1
    assert payload["toplam_mikro_sonra"] == 1

    stale = json.loads(json.dumps(vegetation))
    stale["bolgeler"]["cesme"]["post_onset_tarih"] = "17.09.2026"
    stale_payload = audit(positive, stale)
    stale_rows = stale_payload["bolgeler"]["cesme"]["adaylar"]
    stale_main = next(x for x in stale_rows if round(float(x["enlem"]), 2) == 38.30)
    assert stale_main["baseline_bitki_negatif_baglami"] is False
    assert stale_main["ana_esik_pozitif_destekli_diagnostik_korumali"] is True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("foundation excavation positive context guard self-check: ok")
        return
    payload = audit()
    OUTPUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
