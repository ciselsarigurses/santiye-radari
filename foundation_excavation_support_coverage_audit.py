"""250 m²+ korunmuş temel/kepçe desteklerinin rota zincirinde kaybolup kaybolmadığını denetler.

Yalnız diagnostiktir: alarm/görev üretmez, SQLite'a dokunmaz ve eşikleri değiştirmez.
Pozitif-bağlam katmanındaki korunmuş ana-eşik desteklerini tam saha aday havuzu ve nihai
``gunun_ilk_3_kontrolu`` ile aynı Sentinel-2 sahnesi/konum üzerinden karşılaştırır.
Böylece gelecekte gerçek bir çoklu-kanıt desteği genel BSI rota zincirinde temsil edilmezse
sessizce kaybolmak yerine açık bir recall boşluğu olarak raporlanır.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


BASE = Path(__file__).resolve().parent
CONTEXT_JSON = BASE / "foundation_excavation_positive_context_review.json"
REPORT_JSON = BASE / "latest_report.json"
OUTPUT_JSON = BASE / "foundation_excavation_support_coverage_review.json"
MAIN_THRESHOLD_M2 = 250
MATCH_RADIUS_M = 60.0


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
    lat1, lon1 = a
    lat2, lon2 = b
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _date(value):
    text = str(value or "").strip()
    if not text:
        return ""
    parts = text.replace(".", "-").split("-")
    if len(parts) != 3:
        return ""
    if len(parts[0]) == 4:
        return f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
    return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"


def _region_family(value):
    """Normalize presentation labels without merging Çeşme and Uzunkuyu corridors."""
    text = str(value or "").casefold()
    if "uzunkuyu" in text or "germiyan" in text or "ıldır" in text or "ildir" in text:
        return "uzunkuyu"
    if "çeşme" in text or "cesme" in text or "alaçatı" in text or "alacati" in text or "ılıca" in text or "ilica" in text:
        return "cesme"
    return text.strip()


def _supports(context):
    rows = []
    for region_key, region in (context.get("bolgeler") or {}).items():
        if not isinstance(region, dict):
            continue
        scene = _date(region.get("pozitif_s2_son_tarih"))
        for raw in region.get("adaylar") or []:
            if not isinstance(raw, dict) or _point(raw) is None:
                continue
            if raw.get("ana_esik_pozitif_destekli_diagnostik_korumali") is not True:
                continue
            try:
                area = float(raw.get("spektral_etki_alani_m2") or 0)
            except (TypeError, ValueError):
                area = 0.0
            if area < MAIN_THRESHOLD_M2 or not scene:
                continue
            rows.append({
                "region_key": str(region_key),
                "bolge": str(region.get("bolge") or ""),
                "scene": scene,
                "enlem": float(raw["enlem"]),
                "boylam": float(raw["boylam"]),
                "alan_m2": round(area),
                "morfoloji_puani": raw.get("morfoloji_puani"),
                "pozitif_kanit_kaynaklari": raw.get("pozitif_kanit_kaynaklari") or [],
            })
    return rows


def _nearest(support, rows):
    target = (support["enlem"], support["boylam"])
    nearest = None
    nearest_distance = None
    support_family = _region_family(support.get("bolge"))
    for raw in rows or []:
        if not isinstance(raw, dict) or _point(raw) is None:
            continue
        if _date(raw.get("son_tarih")) != support["scene"]:
            continue
        row_family = _region_family(raw.get("bolge"))
        if support_family and row_family and support_family != row_family:
            continue
        distance = _distance_m(target, _point(raw))
        if nearest_distance is None or distance < nearest_distance:
            nearest = raw
            nearest_distance = distance
    if nearest is None or nearest_distance is None or nearest_distance > MATCH_RADIUS_M:
        return None, nearest_distance
    return nearest, nearest_distance


def audit(context=None, report=None):
    context = context if isinstance(context, dict) else _load(CONTEXT_JSON)
    report = report if isinstance(report, dict) else _load(REPORT_JSON)
    supports = _supports(context)
    pool = report.get("saha_adaylari") or []
    final_route = report.get("gunun_ilk_3_kontrolu") or []

    rows = []
    for support in supports:
        pool_match, pool_distance = _nearest(support, pool)
        route_match, route_distance = _nearest(support, final_route)
        pool_gap = pool_match is None
        route_gap = route_match is None
        recovered = pool_gap and not route_gap
        unrecovered = pool_gap and route_gap
        rows.append({
            **support,
            "tam_aday_havuzunda_temsil": pool_match is not None,
            "tam_aday_havuzu_mesafe_m": round(pool_distance, 1) if pool_distance is not None else None,
            "nihai_rotada_temsil": route_match is not None,
            "nihai_rota_mesafe_m": round(route_distance, 1) if route_distance is not None else None,
            "aday_havuzu_recall_boslugu": pool_gap,
            "rota_recall_boslugu": route_gap,
            "operasyonel_olarak_kurtarildi": recovered,
            "kurtarilamamis_recall_boslugu": unrecovered,
            "alarm": False,
            "saha_gorevi": False,
        })

    pool_orphans = [x for x in rows if x["aday_havuzu_recall_boslugu"]]
    route_orphans = [x for x in rows if x["rota_recall_boslugu"]]
    recovered = [x for x in rows if x["operasyonel_olarak_kurtarildi"]]
    unrecovered = [x for x in rows if x["kurtarilamamis_recall_boslugu"]]
    return {
        "surum": 3,
        "amac": "Korunmuş 250 m²+ çoklu-pozitif temel/kepçe desteklerinin aday/rota zincirinde sessizce kaybolmasını denetlemek",
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "eslesme_yaricapi_m": MATCH_RADIUS_M,
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "korunmus_ana_destek_sayisi": len(rows),
        "aday_havuzu_recall_boslugu_sayisi": len(pool_orphans),
        "rota_recall_boslugu_sayisi": len(route_orphans),
        "operasyonel_kurtarilan_sayisi": len(recovered),
        "kurtarilamamis_recall_boslugu_sayisi": len(unrecovered),
        "ciddi_recall_boslugu": bool(unrecovered),
        "destekler": rows,
        "not": (
            "Bu denetim yeni görev üretmez. Aday-havuzu recall boşluğu ham saha_adaylari üretimindeki kör noktayı "
            "gösterir. Aynı korunmuş destek nihai rotada güvenli kapı üzerinden temsil ediliyorsa operasyonel olarak "
            "kurtarılmış sayılır ve tek başına ciddi sistem sorunu değildir. Bölge sunum etiketleri Çeşme/Uzunkuyu "
            "koridor ailesine normalize edilir; toplu etikette Gülbahçe adının bulunması tek başına Uzunkuyu eşleşmesini "
            "bozmaz. Ciddi recall boşluğu yalnız ham havuzda temsil edilmeyen korunmuş desteğin nihai rotada da "
            "bulunmaması halinde işaretlenir."
        ),
    }


def _self_check():
    context = {
        "bolgeler": {
            "cesme": {
                "bolge": "Çeşme merkez · Alaçatı · Ilıca",
                "pozitif_s2_son_tarih": "18.09.2026",
                "adaylar": [
                    {
                        "enlem": 38.3000,
                        "boylam": 26.3000,
                        "spektral_etki_alani_m2": 400,
                        "morfoloji_puani": 95,
                        "pozitif_kanit_kaynaklari": ["S2_MORFOLOJI", "S1_LOKAL_POZITIF"],
                        "ana_esik_pozitif_destekli_diagnostik_korumali": True,
                    },
                    {
                        "enlem": 38.3010,
                        "boylam": 26.3010,
                        "spektral_etki_alani_m2": 200,
                        "ana_esik_pozitif_destekli_diagnostik_korumali": False,
                    },
                ],
            }
        }
    }
    represented = {
        "saha_adaylari": [{
            "enlem": 38.3001,
            "boylam": 26.3001,
            "son_tarih": "18.09.2026",
            "bolge": "Çeşme merkez · Alaçatı · Ilıca",
        }],
        "gunun_ilk_3_kontrolu": [],
    }
    payload = audit(context, represented)
    assert payload["korunmus_ana_destek_sayisi"] == 1, payload
    assert payload["aday_havuzu_recall_boslugu_sayisi"] == 0, payload
    assert payload["rota_recall_boslugu_sayisi"] == 1, payload
    assert payload["operasyonel_kurtarilan_sayisi"] == 0, payload
    assert payload["kurtarilamamis_recall_boslugu_sayisi"] == 0, payload
    assert payload["ciddi_recall_boslugu"] is False, payload

    recovered = audit(context, {
        "saha_adaylari": [],
        "gunun_ilk_3_kontrolu": [{
            "enlem": 38.3001,
            "boylam": 26.3001,
            "son_tarih": "18.09.2026",
            "bolge": "Çeşme merkez · Alaçatı · Ilıca",
        }],
    })
    assert recovered["aday_havuzu_recall_boslugu_sayisi"] == 1, recovered
    assert recovered["rota_recall_boslugu_sayisi"] == 0, recovered
    assert recovered["operasyonel_kurtarilan_sayisi"] == 1, recovered
    assert recovered["kurtarilamamis_recall_boslugu_sayisi"] == 0, recovered
    assert recovered["ciddi_recall_boslugu"] is False, recovered

    missing = audit(context, {"saha_adaylari": [], "gunun_ilk_3_kontrolu": []})
    assert missing["aday_havuzu_recall_boslugu_sayisi"] == 1, missing
    assert missing["rota_recall_boslugu_sayisi"] == 1, missing
    assert missing["operasyonel_kurtarilan_sayisi"] == 0, missing
    assert missing["kurtarilamamis_recall_boslugu_sayisi"] == 1, missing
    assert missing["ciddi_recall_boslugu"] is True, missing

    # Uzunkuyu toplu kaynak etiketi Gülbahçe'yi içerebilir; rota etiketi bilinçli olarak
    # çekirdek koridora indirgenir. Aynı tarih ve 60 m içinde bu iki sunum etiketi eşleşmelidir.
    grouped_context = {
        "bolgeler": {
            "uzunkuyu": {
                "bolge": "Uzunkuyu · Germiyan · Ildır · Gülbahçe",
                "pozitif_s2_son_tarih": "18.09.2026",
                "adaylar": [{
                    "enlem": 38.262806,
                    "boylam": 26.477305,
                    "spektral_etki_alani_m2": 300,
                    "morfoloji_puani": 90,
                    "pozitif_kanit_kaynaklari": ["S2_MORFOLOJI", "S1_LOKAL_POZITIF"],
                    "ana_esik_pozitif_destekli_diagnostik_korumali": True,
                }],
            }
        }
    }
    grouped_report = {
        "saha_adaylari": [],
        "gunun_ilk_3_kontrolu": [{
            "enlem": 38.262806,
            "boylam": 26.477305,
            "son_tarih": "18.09.2026",
            "bolge": "Uzunkuyu · Germiyan · Ildır",
        }],
    }
    grouped = audit(grouped_context, grouped_report)
    assert grouped["rota_recall_boslugu_sayisi"] == 0, grouped
    assert grouped["operasyonel_kurtarilan_sayisi"] == 1, grouped
    assert grouped["ciddi_recall_boslugu"] is False, grouped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("foundation excavation support coverage self-check: ok")
        return
    payload = audit()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
