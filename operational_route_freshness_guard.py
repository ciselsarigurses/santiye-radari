"""Operasyonel saha rotasında gerçek uydu kanıtı tazeliğini korur.

Bu katman yeni alarm veya saha görevi üretmez; 250 m² ana Sentinel eşiğini ve
150–249 m² MİKRO politikasını değiştirmez. Yalnız günlük saha rotasını sıralar.

Sorun: ``GECİKEN`` etiketi görev/backlog yaşını anlatır; uydu kanıtının ne kadar
yeni olduğunu tek başına anlatmaz. Aynı küçük-güçlü sınıfta çok eski bir görev,
daha yeni bir zemin hareketinin önüne geçebiliyordu. Bu guard, gecikmiş görevlerde
önce şantiye-ölçeği sınıfını, sonra gerçek Sentinel kanıt yaşını kullanır. Böylece
2 günlük güçlü bir kazı/toprak değişimi, aynı sınıftaki 13 günlük eski backlog'un
önüne geçer. İnsan tarafından ``TEKRAR_GIT`` istenen kayıtlar her zaman korunur.

Geometri guard'ı önce uygulanır; düşük-kompakt/uzun-ince risk nedeniyle BEKLEYEN'e
alınan kayıtlar bu katmanda tekrar yükseltilmez. Gülbahçe, doğu Sentinel bölgesinin
mevcut kapsama dengesi içinde aynen korunur.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from daily_route_shortlist import (
    SATELLITE_PRIORITY,
    _actionable_candidates,
    _balance_satellite_regions,
    _overdue_excavation_class,
    _route_priority_value,
)
from operational_route_shape_guard import build_operational_route


REPORT_JSON = Path(__file__).with_name("latest_report.json")
SHAPE_REVIEW_JSON = Path(__file__).with_name("site_scale_shape_risk_review.json")
OUTPUT_JSON = Path(__file__).with_name("operational_route.json")
FINAL_LIMIT = 3
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_date(value):
    text = str(value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _evidence_age_days(item, report_day):
    """Uydu kanıt yaşını görev yaşından bağımsız çıkar."""
    raw = item.get("uydu_kanit_yasi_gun")
    try:
        if raw is not None:
            return max(int(raw), 0)
    except (TypeError, ValueError):
        pass

    scene_day = _parse_date(item.get("son_tarih"))
    if report_day is None or scene_day is None:
        return 999
    return max((report_day - scene_day).days, 0)


def _fresh_sort_key(item, original_index, report_day):
    status = str(item.get("saha_durumu") or "KONTROLE_GIT").strip().upper()
    if status == "TEKRAR_GIT":
        # İnsan geri bildirimi otomatik rota optimizasyonundan üstündür.
        return (-1, 0, 0, 0, 0.0, 0, original_index)

    priority = _route_priority_value(item)
    overdue = str(item.get("oncelik") or "").strip().upper() == "GECİKEN"
    if not overdue:
        return (priority, 0, 0, 0, 0.0, 0, original_index)

    excavation_class = _overdue_excavation_class(item)
    evidence_age = _evidence_age_days(item, report_day)
    satellite_priority = SATELLITE_PRIORITY.get(
        str(item.get("uydu_onceligi") or "").strip().upper(), 3
    )
    area = max(_number(item.get("alan_m2"), 0), 0)
    waiting_days = max(int(_number(item.get("bekleme_gun"), 0)), 0)

    # Önce şantiye ölçeği/karakteri, sonra gerçek kanıt tazeliği. Alan yalnız
    # aynı sınıf ve aynı tazelikte bağlayıcıdır; eski küçük alan yeni kanıtı ezmez.
    return (
        priority,
        excavation_class,
        evidence_age,
        satellite_priority,
        area,
        waiting_days,
        original_index,
    )


def select_fresh_shortlist(candidates, report_day, limit=FINAL_LIMIT):
    cap = max(int(limit), 0)
    if cap <= 0:
        return []

    eligible = _actionable_candidates(candidates)
    indexed = list(enumerate(eligible))
    indexed.sort(key=lambda pair: _fresh_sort_key(pair[1], pair[0], report_day))
    ranked = [item for _, item in indexed]

    selected = [dict(item) for item in ranked[:cap]]
    selected = _balance_satellite_regions(ranked, selected, cap)
    for index, item in enumerate(selected, start=1):
        item["gunluk_sira"] = index
        item["rota_uydu_kanit_yasi_gun"] = _evidence_age_days(item, report_day)
    return selected


def build_fresh_operational_route(report, shape_review, limit=FINAL_LIMIT):
    report_day = _parse_date(report.get("rapor_tarihi"))
    candidates = report.get("saha_adaylari") or []
    candidate_count = len(candidates) if isinstance(candidates, list) else 0

    # Önce mevcut geometri korumasının gerçek nihai 3'lüsünü üret.
    base = build_operational_route(report, shape_review, limit=limit)
    shape_ids = [str(item.get("gorev_id") or "") for item in base.get("operasyonel_rota", [])]

    # Sonra geometri guard uygulanmış tüm aktif aday havuzunu al. Bu çağrı alarm
    # veya görev üretmez; yalnız route seçim havuzunu genişletir.
    expanded_limit = max(candidate_count, limit, 1)
    expanded = build_operational_route(report, shape_review, limit=expanded_limit)
    expanded_candidates = expanded.get("operasyonel_rota") or []
    selected = select_fresh_shortlist(expanded_candidates, report_day, limit=limit)
    fresh_ids = [str(item.get("gorev_id") or "") for item in selected]

    result = dict(base)
    result["operasyonel_rota"] = selected
    result["tazelik_korumasi"] = True
    result["tazelik_sirasi_degisti"] = shape_ids != fresh_ids
    result["geometri_sonrasi_rota_gorevleri"] = shape_ids
    result["tazelik_sonrasi_rota_gorevleri"] = fresh_ids
    result["ana_sentinel_esigi_m2"] = MAIN_THRESHOLD_M2
    result["mikro_aralik_m2"] = MICRO_RANGE_M2
    result["rota_degisti"] = bool(base.get("rota_degisti")) or shape_ids != fresh_ids
    result["tazelik_notu"] = (
        "Yeni alarm/görev üretmez. GECİKEN görevlerde önce küçük-güçlü/parsel ölçeği, "
        "sonra gerçek Sentinel kanıt yaşı kullanılır; daha eski backlog aynı sınıftaki "
        "daha yeni zemin hareketinin önüne geçemez. TEKRAR_GIT ve geometri arka-plan "
        "koruması aynen korunur."
    )
    return result


def _load_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _self_check():
    west = "Çeşme merkez · Alaçatı · Ilıca"
    report = {
        "rapor_tarihi": "2026-09-10",
        "saha_adaylari": [
            {
                "gorev_id": "STALE_SMALLER",
                "saha_durumu": "KONTROLE_GIT",
                "oncelik": "GECİKEN",
                "mahalle": "Alaçatı",
                "enlem": 38.282433,
                "boylam": 26.365212,
                "alan_m2": 300,
                "boyut_sinifi": "KUCUK",
                "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
                "uydu_onceligi": "ORTA",
                "son_tarih": "05.09.2026",
                "bekleme_gun": 5,
                "bolge": west,
            },
            {
                "gorev_id": "VERY_OLD",
                "saha_durumu": "KONTROLE_GIT",
                "oncelik": "GECİKEN",
                "mahalle": "Musalla",
                "enlem": 38.294734,
                "boylam": 26.311523,
                "alan_m2": 400,
                "boyut_sinifi": "KUCUK",
                "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
                "uydu_onceligi": "ORTA",
                "son_tarih": "28.08.2026",
                "bekleme_gun": 13,
                "bolge": west,
            },
            {
                "gorev_id": "FRESH",
                "saha_durumu": "KONTROLE_GIT",
                "oncelik": "GECİKEN",
                "mahalle": "Gülbahçe",
                "enlem": 38.331547,
                "boylam": 26.644338,
                "alan_m2": 400,
                "boyut_sinifi": "KUCUK",
                "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
                "uydu_onceligi": "ORTA",
                "son_tarih": "08.09.2026",
                "uydu_kanit_yasi_gun": 2,
                "bekleme_gun": 2,
                "bolge": "Uzunkuyu · Germiyan · Ildır · Gülbahçe",
            },
        ],
    }
    result = build_fresh_operational_route(report, {}, limit=3)
    ids = [item["gorev_id"] for item in result["operasyonel_rota"]]
    assert ids[0] == "FRESH", "2 günlük güçlü kanıt eski küçük backlog'un gerisinde kalmamalı."
    assert ids.index("STALE_SMALLER") < ids.index("VERY_OLD")
    assert result["ana_sentinel_esigi_m2"] == 250
    assert result["mikro_aralik_m2"] == [150, 249]

    with_repeat = dict(report)
    with_repeat["saha_adaylari"] = [
        {
            **report["saha_adaylari"][1],
            "gorev_id": "HUMAN_RECHECK",
            "saha_durumu": "TEKRAR_GIT",
        },
        *report["saha_adaylari"],
    ]
    repeated = build_fresh_operational_route(with_repeat, {}, limit=3)
    assert repeated["operasyonel_rota"][0]["gorev_id"] == "HUMAN_RECHECK"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()

    if args.self_check:
        _self_check()
        print("Operasyonel rota tazelik koruması öz testi başarılı.")
    else:
        report = _load_json(REPORT_JSON)
        shape_review = _load_json(SHAPE_REVIEW_JSON)
        result = build_fresh_operational_route(report, shape_review, limit=FINAL_LIMIT)
        OUTPUT_JSON.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        ids = result.get("tazelik_sonrasi_rota_gorevleri") or []
        print("Tazelik korumalı operasyonel rota: " + (", ".join(ids) or "aktif görev yok"))
