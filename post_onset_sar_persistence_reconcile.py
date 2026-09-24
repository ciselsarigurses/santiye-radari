"""Post-onset SAR süreklilik kanıtını onset kanıtından ayrı uzlaştırır.

Bu katman yalnız diagnostiktir; alarm, saha görevi veya rota üretmez. Amaç,
13 Eylül sakin baseline -> 17 Eylül sonrası S2 morfoloji değişimi bulunan bir adayda
Sentinel-1 çifti baseline tarihini çevrelemese bile 15-17 Eylül başlangıç penceresinden
başlayıp post-onset S2 sahnesini aşan güçlü, çift-pol ve kompakt lokal değişimin
"devam eden müdahale" kanıtı olarak görünür olmasını sağlamaktır.

Bu destek onset başlangıç tarihini kanıtlamaz. Yalnız S2 morfolojisi + bağımsız SAR
sürekliliği birlikteliğini diagnostik olarak işaretler. 250 m² ana eşik korunur;
150-249 m² yalnız MİKRO diagnostiktir. Tarla/bahçe, saha yanlış-pozitifi ve mevcut
müşteri engelleri korunur.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path

BASE = Path(__file__).resolve().parent
BRIDGE_JSON = BASE / "post_onset_baseline_sar_bridge_review.json"
CROSS_JSON = BASE / "post_onset_baseline_sar_cross_review.json"
FEEDBACK_JSON = BASE / "manual_field_feedback.json"
OUTPUT_JSON = BASE / "post_onset_sar_persistence_review.json"

MAIN_THRESHOLD_M2 = 250
MICRO_MIN_M2 = 150
MICRO_MAX_M2 = 249
MATCH_RADIUS_M = 15.0
DEFAULT_FEEDBACK_RADIUS_M = 30.0
STRONG_SAR_DB = 2.0
SPECIAL_ONSET_START = "2026-09-15"


def _load(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _date(value):
    text = str(value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _point(item):
    try:
        return float(item["enlem"]), float(item["boylam"])
    except (KeyError, TypeError, ValueError):
        return None


def _distance_m(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _nearest(target, rows):
    tp = _point(target)
    if tp is None:
        return None, None
    best = None
    best_distance = None
    for row in rows:
        rp = _point(row)
        if rp is None:
            continue
        distance = _distance_m(tp, rp)
        if best_distance is None or distance < best_distance:
            best = row
            best_distance = distance
    return best, best_distance


def _strong_compact_dual(row):
    try:
        score = float(row.get("sar_lokal_degisim_skor_db"))
    except (TypeError, ValueError):
        return False
    return bool(
        score >= STRONG_SAR_DB
        and str(row.get("sar_polarizasyon_uyumu") or "") == "CIFT_POL_GUCLU"
        and str(row.get("sar_mekansal_ayrim") or "") == "KOMPAKT_LOKAL_DESTEKLI"
    )


def _spans_post_scene(sar_old, sar_new, post_scene):
    old_date = _date(sar_old)
    new_date = _date(sar_new)
    post_date = _date(post_scene)
    onset_start = _date(SPECIAL_ONSET_START)
    return bool(
        old_date
        and new_date
        and post_date
        and onset_start
        and onset_start <= old_date < post_date <= new_date
    )


def _feedback_block(candidate, scene_date, feedback):
    cp = _point(candidate)
    if cp is None:
        return False, None
    matches = []
    for item in feedback:
        ip = _point(item)
        if ip is None:
            continue
        try:
            radius = float(item.get("eslesme_yaricapi_m") or DEFAULT_FEEDBACK_RADIUS_M)
        except (TypeError, ValueError):
            radius = DEFAULT_FEEDBACK_RADIUS_M
        distance = _distance_m(cp, ip)
        if distance <= max(radius, 1.0):
            matches.append((distance, item))
    if not matches:
        return False, None
    distance, item = min(matches, key=lambda x: x[0])
    outcome = str(item.get("sonuc") or "").upper()
    result_date = _date(item.get("sonuc_tarihi"))
    same_or_older_evidence = bool(scene_date and result_date and scene_date <= result_date)
    blocked = outcome == "MEVCUT_MUSTERI" or (
        outcome == "YANLIS_POZITIF" and same_or_older_evidence
    )
    return blocked, {
        "id": item.get("id"),
        "sonuc": outcome,
        "mesafe_m": round(distance, 1),
        "sonuc_tarihi": item.get("sonuc_tarihi"),
        "ayni_veya_eski_kanit": same_or_older_evidence,
    }


def _reconcile_region(region_key, bridge_region, cross_region, feedback):
    bridge_rows = [
        x for x in bridge_region.get("adaylar") or []
        if isinstance(x, dict) and _point(x) is not None
    ]
    cross_rows = [
        x for x in cross_region.get("adaylar") or []
        if isinstance(x, dict) and _point(x) is not None
    ]
    post_scene = bridge_region.get("post_onset_tarih") or cross_region.get("post_onset_tarih")
    post_scene_date = _date(post_scene)
    rows = []

    for bridge in bridge_rows:
        cross, distance = _nearest(bridge, cross_rows)
        if cross is None or distance is None or distance > MATCH_RADIUS_M:
            continue
        try:
            area = int(bridge.get("s2_spektral_etki_alani_m2") or 0)
            morphology = float(bridge.get("s2_seed_morfoloji_puani") or 0)
        except (TypeError, ValueError):
            continue

        morphology_high = bool(
            morphology >= 65
            and str(bridge.get("s2_seed_morfoloji_seviyesi") or "").upper() == "YUKSEK"
        )
        agri_block = bool(
            bridge.get("saha_negatif_eslesme")
            or (bridge.get("tarla_bahce_negatif_nedenler") or [])
            or float(bridge.get("kalici_bitki_orani_5x5") or 0) >= 0.40
        )
        field_block, feedback_row = _feedback_block(bridge, post_scene_date, feedback)
        persistence = bool(
            _strong_compact_dual(bridge)
            and _spans_post_scene(
                bridge.get("sar_eski_tarih"),
                bridge.get("sar_yeni_tarih"),
                post_scene,
            )
        )
        cross_onset_support = cross.get("sar_cifti_baseline_onseti_cevreliyor") is True
        already_high = cross.get("yuksek_guven_diagnostik") is True
        recovered = bool(
            morphology_high
            and persistence
            and not cross_onset_support
            and not already_high
            and not agri_block
            and not field_block
        )
        main = area >= MAIN_THRESHOLD_M2
        micro = MICRO_MIN_M2 <= area <= MICRO_MAX_M2

        rows.append({
            "enlem": bridge.get("enlem"),
            "boylam": bridge.get("boylam"),
            "spektral_etki_alani_m2": area,
            "morfoloji_puani": morphology,
            "morfoloji_yuksek": morphology_high,
            "sar_eski_tarih": bridge.get("sar_eski_tarih"),
            "sar_yeni_tarih": bridge.get("sar_yeni_tarih"),
            "sar_lokal_degisim_skor_db": bridge.get("sar_lokal_degisim_skor_db"),
            "sar_polarizasyon_uyumu": bridge.get("sar_polarizasyon_uyumu"),
            "sar_mekansal_ayrim": bridge.get("sar_mekansal_ayrim"),
            "sar_post_onset_sureklilik_destek": persistence,
            "sar_onset_baslangici_kaniti": False,
            "cross_onset_penceresi_destek": cross_onset_support,
            "cross_eslesme_mesafe_m": round(distance, 1),
            "tarla_bahce_engeli": agri_block,
            "geri_bildirim_engeli": field_block,
            "geri_bildirim": feedback_row,
            "sureklilik_ile_kurtarilan": recovered,
            "ana_esik": main,
            "mikro": micro,
            "yuksek_guven_diagnostik": bool(recovered and main),
            "mikro_coklu_kanit_diagnostik": bool(recovered and micro),
            "alarm": False,
            "saha_gorevi": False,
        })

    rows.sort(
        key=lambda x: (
            x["yuksek_guven_diagnostik"],
            x["mikro_coklu_kanit_diagnostik"],
            x["sureklilik_ile_kurtarilan"],
            float(x.get("morfoloji_puani") or 0),
            float(x.get("sar_lokal_degisim_skor_db") or 0),
        ),
        reverse=True,
    )
    return {
        "bolge": bridge_region.get("bolge") or cross_region.get("bolge") or region_key,
        "baseline_tarih": bridge_region.get("baseline_tarih") or cross_region.get("baseline_tarih"),
        "post_onset_tarih": post_scene,
        "girdi_bridge_aday": len(bridge_rows),
        "eslesen_cross_aday": len(rows),
        "sureklilik_ile_kurtarilan_sayi": sum(1 for x in rows if x["sureklilik_ile_kurtarilan"]),
        "ana_esik_yuksek_guven_sayi": sum(1 for x in rows if x["yuksek_guven_diagnostik"]),
        "mikro_coklu_kanit_sayi": sum(1 for x in rows if x["mikro_coklu_kanit_diagnostik"]),
        "adaylar": rows,
    }


def audit(bridge=None, cross=None, feedback=None):
    bridge = bridge if isinstance(bridge, dict) else _load(BRIDGE_JSON)
    cross = cross if isinstance(cross, dict) else _load(CROSS_JSON)
    feedback = feedback if isinstance(feedback, dict) else _load(FEEDBACK_JSON)
    feedback_rows = [x for x in feedback.get("kayitlar") or [] if isinstance(x, dict)]
    regions = {}
    keys = set((bridge.get("bolgeler") or {}).keys()) | set((cross.get("bolgeler") or {}).keys())
    for key in sorted(keys):
        regions[key] = _reconcile_region(
            key,
            (bridge.get("bolgeler") or {}).get(key) or {},
            (cross.get("bolgeler") or {}).get(key) or {},
            feedback_rows,
        )
    main_count = sum(x["ana_esik_yuksek_guven_sayi"] for x in regions.values())
    micro_count = sum(x["mikro_coklu_kanit_sayi"] for x in regions.values())
    return {
        "surum": 1,
        "amac": "Onset çevreleme kanıtı ile post-onset SAR süreklilik kanıtını ayırarak recall körlüğünü diagnostik olarak ölçmek",
        "gercek_derinlik_olcumu": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": [MICRO_MIN_M2, MICRO_MAX_M2],
        "sar_guclu_esik_db": STRONG_SAR_DB,
        "post_onset_sureklilik_polarizasyon_kapisi": "CIFT_POL_GUCLU",
        "post_onset_sureklilik_mekansal_kapi": "KOMPAKT_LOKAL_DESTEKLI",
        "toplam_ana_esik_yuksek_guven_diagnostik": main_count,
        "toplam_mikro_coklu_kanit_diagnostik": micro_count,
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "bolgeler": regions,
        "not": "Bu çıktı rotaya enjeksiyon yapmaz. SAR çifti 13 Eylül baseline'ını çevrelemiyorsa onset başlangıcı kanıtı sayılmaz. Ancak 15 Eylül veya sonrasında başlayıp post-onset S2 sahnesini aşan >=2 dB, çift-pol güçlü ve kompakt-lokal SAR değişimi; yüksek S2 morfolojisi ve negatif bağlam yokluğuyla birlikte yalnız 'devam eden müdahale' ikinci kanıtı olarak diagnostik sayılır.",
    }


def _self_check():
    assert _strong_compact_dual({
        "sar_lokal_degisim_skor_db": 2.5,
        "sar_polarizasyon_uyumu": "CIFT_POL_GUCLU",
        "sar_mekansal_ayrim": "KOMPAKT_LOKAL_DESTEKLI",
    })
    assert not _strong_compact_dual({
        "sar_lokal_degisim_skor_db": 2.5,
        "sar_polarizasyon_uyumu": "CIFT_POL_ORTA",
        "sar_mekansal_ayrim": "KOMPAKT_LOKAL_DESTEKLI",
    })
    assert _spans_post_scene("2026-09-17", "2026-09-23", "18.09.2026")
    assert not _spans_post_scene("2026-09-14", "2026-09-23", "18.09.2026")

    bridge = {
        "bolgeler": {
            "cesme": {
                "bolge": "Çeşme",
                "baseline_tarih": "13.09.2026",
                "post_onset_tarih": "18.09.2026",
                "adaylar": [{
                    "enlem": 38.30,
                    "boylam": 26.30,
                    "s2_spektral_etki_alani_m2": 500,
                    "s2_seed_morfoloji_puani": 100,
                    "s2_seed_morfoloji_seviyesi": "YUKSEK",
                    "kalici_bitki_orani_5x5": 0.0,
                    "tarla_bahce_negatif_nedenler": [],
                    "saha_negatif_eslesme": None,
                    "sar_eski_tarih": "2026-09-17",
                    "sar_yeni_tarih": "2026-09-23",
                    "sar_lokal_degisim_skor_db": 3.7,
                    "sar_polarizasyon_uyumu": "CIFT_POL_GUCLU",
                    "sar_mekansal_ayrim": "KOMPAKT_LOKAL_DESTEKLI",
                }],
            }
        }
    }
    cross = {
        "bolgeler": {
            "cesme": {
                "post_onset_tarih": "18.09.2026",
                "adaylar": [{
                    "enlem": 38.30,
                    "boylam": 26.30,
                    "sar_cifti_baseline_onseti_cevreliyor": False,
                    "yuksek_guven_diagnostik": False,
                }],
            }
        }
    }
    result = audit(bridge=bridge, cross=cross, feedback={"kayitlar": []})
    assert result["toplam_ana_esik_yuksek_guven_diagnostik"] == 1
    row = result["bolgeler"]["cesme"]["adaylar"][0]
    assert row["sar_post_onset_sureklilik_destek"] is True
    assert row["sar_onset_baslangici_kaniti"] is False
    assert row["yuksek_guven_diagnostik"] is True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("post-onset SAR persistence reconcile self-check: ok")
        return 0
    payload = audit()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
