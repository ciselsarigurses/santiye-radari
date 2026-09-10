"""Gecikmiş saha rotasında geometrik yanlış-pozitif riskini temkinli biçimde azaltır.

Bu katman Sentinel alarmı üretmez, görevi açmaz/kapatmaz, ana 250 m² eşiğini veya
150–249 m² MİKRO politikasını değiştirmez. Yalnız ``GECİKEN`` durumundaki
800–10.000 m² şantiye-ölçeği adaylarından, güncel aday sahnesiyle aynı Sentinel
sahnesinde düşük kompaktlık/uzun-ince şekil riski doğrulanmış olanları rota
hesabında ``BEKLEYEN`` düzeyine indirir. Kaydın kendisi ``latest_report.json``
içinde değiştirilmez; arka-plan takibi devam eder.

Amaç büyük ama dağınık/doğal yüzey hareketlerinin sınırlı günlük saha kapasitesini
kaplamasını azaltırken taze ERKEN/PARSEL sinyallerini ve insan tarafından istenen
TEKRAR_GIT kontrollerini tamamen korumaktır.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime
from pathlib import Path

from daily_route_shortlist import select_shortlist


REPORT_JSON = Path(__file__).with_name("latest_report.json")
SHAPE_REVIEW_JSON = Path(__file__).with_name("site_scale_shape_risk_review.json")
OUTPUT_JSON = Path(__file__).with_name("operational_route.json")
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
SHAPE_MIN_M2 = 800
SHAPE_MAX_M2 = 10000
MATCH_DISTANCE_M = 25.0
MIN_AREA_RATIO = 0.85
ACTIVE_STATUSES = {"KONTROLE_GIT", "TEKRAR_GIT"}


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _point(item):
    try:
        return float(item.get("enlem")), float(item.get("boylam"))
    except (TypeError, ValueError, AttributeError):
        return None


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _area_ratio(first, second):
    first = max(_number(first, 0), 0)
    second = max(_number(second, 0), 0)
    if not first or not second:
        return 0.0
    return min(first, second) / max(first, second)


def _parse_candidate_date(value):
    text = str(value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _scene_date(item_id):
    match = re.search(r"_(\d{8})T\d{6}", str(item_id or ""))
    if not match:
        return ""
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date().isoformat()
    except ValueError:
        return ""


def _load_json(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def shape_risks(review):
    """Koordinatlı nihai şekil-risklerini, bağlı Sentinel sahne tarihiyle çıkar."""
    results = []
    seen = set()
    regions = review.get("bolgeler") or {}
    if not isinstance(regions, dict):
        return results

    for region_key, region in regions.items():
        if not isinstance(region, dict) or str(region.get("durum") or "") != "ok":
            continue
        latest_item = str(region.get("latest_item") or region.get("son_item") or "")
        latest_date = _scene_date(latest_item)
        examples = region.get("nihai_cozulen_ornekler") or region.get("nihai_ornekler") or []
        for raw in examples:
            if not isinstance(raw, dict):
                continue
            point = _point(raw)
            area = _number(raw.get("alan_m2"), 0)
            risk = str(raw.get("sekil_riski") or "").strip().upper()
            if point is None or not (SHAPE_MIN_M2 < area <= SHAPE_MAX_M2):
                continue
            if risk in {"", "YOK", "NONE"} and not (
                bool(raw.get("dusuk_kompaktlik")) or bool(raw.get("uzun_ince"))
            ):
                continue
            key = (round(point[0], 6), round(point[1], 6), int(round(area)))
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "bolge_anahtari": str(region_key),
                    "bolge": str(region.get("bolge") or region_key),
                    "latest_item": latest_item,
                    "latest_date": latest_date,
                    "enlem": round(point[0], 6),
                    "boylam": round(point[1], 6),
                    "alan_m2": int(round(area)),
                    "sekil_riski": risk or "GEOMETRI_RISKI",
                    "kompaktlik": raw.get("kompaktlik"),
                    "uzun_kisa_orani": raw.get("uzun_kisa_orani"),
                }
            )
    return results


def matching_shape_risk(candidate, risks):
    """Sadece aynı sahne ve aynı yer/ölçekteki gecikmiş adayı eşleştir."""
    if str(candidate.get("oncelik") or "").strip().upper() != "GECİKEN":
        return None
    status = str(candidate.get("saha_durumu") or "KONTROLE_GIT").strip().upper()
    if status not in ACTIVE_STATUSES:
        return None
    if status == "TEKRAR_GIT":
        # İnsan tarafından özellikle yeniden görülmek istenen kayıtları asla düşürme.
        return None

    area = _number(candidate.get("alan_m2"), 0)
    if not (SHAPE_MIN_M2 < area <= SHAPE_MAX_M2):
        return None
    point = _point(candidate)
    candidate_date = _parse_candidate_date(candidate.get("son_tarih"))
    if point is None or not candidate_date:
        return None

    for risk in risks:
        risk_date = str(risk.get("latest_date") or "")
        if not risk_date or risk_date != candidate_date:
            continue
        risk_point = _point(risk)
        if risk_point is None or _distance_m(point, risk_point) > MATCH_DISTANCE_M:
            continue
        if _area_ratio(area, risk.get("alan_m2")) < MIN_AREA_RATIO:
            continue
        return risk
    return None


def build_operational_route(report, shape_review, limit=3):
    candidates = report.get("saha_adaylari") or []
    if not isinstance(candidates, list):
        candidates = []
    risks = shape_risks(shape_review)

    adjusted = []
    background = []
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        risk = matching_shape_risk(item, risks)
        if risk is not None:
            item["rota_orijinal_oncelik"] = str(item.get("oncelik") or "")
            item["oncelik"] = "BEKLEYEN"
            item["rota_geometri_arka_plan"] = True
            item["rota_geometri_riski"] = risk.get("sekil_riski")
            background.append(
                {
                    "gorev_id": str(item.get("gorev_id") or ""),
                    "enlem": round(float(item["enlem"]), 6),
                    "boylam": round(float(item["boylam"]), 6),
                    "alan_m2": int(round(_number(item.get("alan_m2"), 0))),
                    "son_tarih": str(item.get("son_tarih") or ""),
                    "sekil_riski": str(risk.get("sekil_riski") or "GEOMETRI_RISKI"),
                    "kompaktlik": risk.get("kompaktlik"),
                    "neden": (
                        "Gecikmiş aday aynı Sentinel sahnesinde düşük-kompakt/uzun-ince "
                        "geometri riski taşıyor; silinmedi, günlük rotada arka planda tutuldu."
                    ),
                }
            )
        adjusted.append(item)

    guarded_route = select_shortlist(adjusted, limit=limit)
    baseline_route = select_shortlist(candidates, limit=limit)
    baseline_ids = [str(item.get("gorev_id") or "") for item in baseline_route]
    guarded_ids = [str(item.get("gorev_id") or "") for item in guarded_route]

    return {
        "rapor_tarihi": str(report.get("rapor_tarihi") or ""),
        "kaynak_rapor_olusturma": str(report.get("olusturma") or ""),
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "rota_limit": int(limit),
        "geometri_riski_eslesme_m": MATCH_DISTANCE_M,
        "geometri_riski_min_alan_orani": MIN_AREA_RATIO,
        "geometri_arka_plan_toplam": len(background),
        "rota_degisti": baseline_ids != guarded_ids,
        "temel_rota_gorevleri": baseline_ids,
        "operasyonel_rota": guarded_route,
        "geometri_arka_plan": background,
        "yorum": (
            "Bu çıktı yeni alarm veya saha görevi üretmez. Yalnız gecikmiş 800–10.000 m² "
            "adaylarda aynı Sentinel sahnesiyle doğrulanmış düşük-kompakt/uzun-ince "
            "geometri riskini günlük rota sıralamasında arka plana alır. Taze ERKEN/PARSEL, "
            "TEKRAR_GIT, 250 m² ana eşik ve 150–249 m² MİKRO politikası değişmez."
        ),
    }


def _self_check():
    scene = "S2B_T35SMC_20260908T090037_L2A"
    review = {
        "bolgeler": {
            "uzunkuyu": {
                "bolge": "Uzunkuyu · Germiyan · Ildır · Gülbahçe",
                "durum": "ok",
                "latest_item": scene,
                "nihai_ornekler": [
                    {
                        "enlem": 38.263077,
                        "boylam": 26.677997,
                        "alan_m2": 6901,
                        "dusuk_kompaktlik": True,
                        "sekil_riski": "DUSUK_KOMPAKTLIK",
                        "kompaktlik": 0.123,
                    }
                ],
            }
        }
    }
    base = {
        "saha_durumu": "KONTROLE_GIT",
        "bolge": "Uzunkuyu · Germiyan · Ildır · Gülbahçe",
        "son_tarih": "08.09.2026",
        "bekleme_gun": 3,
    }
    report = {
        "rapor_tarihi": "2026-09-10",
        "saha_adaylari": [
            {
                **base,
                "gorev_id": "RISK",
                "oncelik": "GECİKEN",
                "enlem": 38.263077,
                "boylam": 26.677997,
                "alan_m2": 6901,
                "uydu_onceligi": "YÜKSEK",
            },
            {
                **base,
                "gorev_id": "CLEAN",
                "oncelik": "GECİKEN",
                "enlem": 38.30,
                "boylam": 26.60,
                "alan_m2": 2500,
                "uydu_onceligi": "ORTA",
            },
            {
                **base,
                "gorev_id": "PARSEL",
                "oncelik": "PARSEL",
                "enlem": 38.31,
                "boylam": 26.61,
                "alan_m2": 900,
                "uydu_onceligi": "YÜKSEK",
            },
        ],
    }
    result = build_operational_route(report, review, limit=3)
    ids = [item["gorev_id"] for item in result["operasyonel_rota"]]
    assert ids[0] == "PARSEL", "Taze PARSEL şekil guard nedeniyle geriye düşmemeli."
    assert ids.index("CLEAN") < ids.index("RISK"), "Temiz gecikmiş aday riskli geniş adaydan önce gelmeli."
    assert result["geometri_arka_plan_toplam"] == 1

    fresh = dict(report["saha_adaylari"][0])
    fresh["oncelik"] = "ERKEN"
    assert matching_shape_risk(fresh, shape_risks(review)) is None, "ERKEN aday asla geometri demotion almamalı."

    repeat = dict(report["saha_adaylari"][0])
    repeat["saha_durumu"] = "TEKRAR_GIT"
    assert matching_shape_risk(repeat, shape_risks(review)) is None, "TEKRAR_GIT insan kararı korunmalı."

    stale = dict(report["saha_adaylari"][0])
    stale["son_tarih"] = "05.09.2026"
    assert matching_shape_risk(stale, shape_risks(review)) is None, "Farklı sahne tarihindeki şekil riski kullanılmamalı."


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        _self_check()
        print("operational_route_shape_guard self-check: OK")
        return

    report = _load_json(REPORT_JSON)
    review = _load_json(SHAPE_REVIEW_JSON)
    result = build_operational_route(report, review)
    OUTPUT_JSON.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "operational route: "
        f"{len(result['operasyonel_rota'])} nokta · "
        f"geometri arka plan {result['geometri_arka_plan_toplam']} · "
        f"rota_degisti={result['rota_degisti']}"
    )


if __name__ == "__main__":
    main()
