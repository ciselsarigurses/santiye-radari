"""Gülbahçe'de en yeni Sentinel durumunun gerçekten görülemediği kör cepleri denetler.

Temporal/yedek sahne iyileştirmesi eski yüzey bilgisini geri kazanabilir; fakat en
son sahne bulut/gölge/geçersiz ise o pikselde yeni başlayan hafriyatın güncel durumu
yine gözlenemez. Bu katman mevcut Gülbahçe mikro-körlük çıktısını kör-alan devriyesi
ile karşılaştırır. Alarm veya saha görevi üretmez, 250 m² ana eşiğini değiştirmez.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


MICRO_BLIND_JSON = Path(__file__).with_name("gulbahce_micro_blind_capacity.json")
COVERAGE_JSON = Path(__file__).with_name("coverage_blind_area_audit.json")
REPORT_JSON = Path(__file__).with_name("latest_report.json")
OUTPUT_JSON = Path(__file__).with_name("gulbahce_latest_state_blind_review.json")

MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
PATROL_MAX_AREA_M2 = 6500
MATCH_RADIUS_M = 35


def _point(item):
    try:
        return float(item.get("enlem")), float(item.get("boylam"))
    except (AttributeError, TypeError, ValueError):
        return None


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def build_review(micro_payload, coverage_payload, report_payload):
    coverage_east = ((coverage_payload or {}).get("bolgeler") or {}).get("uzunkuyu") or {}
    source_date = str((micro_payload or {}).get("kaynak_son_tarih") or "")
    source_item = str((micro_payload or {}).get("kaynak_son_item") or "")
    coverage_date = str(coverage_east.get("son_tarih") or "")
    coverage_item = str(coverage_east.get("son_item") or "")
    same_scene = bool(source_date and source_item and source_date == coverage_date and source_item == coverage_item)

    current_blind = []
    for raw in (micro_payload or {}).get("kor_kume_ornekleri") or []:
        if not isinstance(raw, dict):
            continue
        point = _point(raw)
        try:
            area = int(round(float(raw.get("alan_m2") or 0)))
        except (TypeError, ValueError):
            area = 0
        if point is None or not (MAIN_THRESHOLD_M2 <= area <= PATROL_MAX_AREA_M2):
            continue
        current_blind.append(
            {
                "enlem": round(point[0], 6),
                "boylam": round(point[1], 6),
                "alan_m2": area,
                "neden": str(raw.get("neden") or "KALITE_KORLUGU"),
                "alarm": False,
                "saha_gorevi": False,
            }
        )

    guard = (report_payload or {}).get("gulbahce_kor_alan_devriye_korumasi") or {}
    selected = guard.get("secilen") if isinstance(guard, dict) else None
    selected_point = _point(selected) if isinstance(selected, dict) else None

    covered = []
    for item in current_blind:
        point = _point(item)
        distance = _distance_m(point, selected_point) if point and selected_point else None
        row = dict(item)
        row["secilen_devriyeye_mesafe_m"] = round(distance) if distance is not None else None
        row["secilen_devriye_kapsiyor"] = bool(distance is not None and distance <= MATCH_RADIUS_M)
        covered.append(row)

    matched_count = sum(1 for item in covered if item["secilen_devriye_kapsiyor"])
    status = "ok" if same_scene else "veri_tarihi_uyusmuyor"
    return {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_santiye_araligi_m2": MICRO_RANGE_M2,
        "durum": status,
        "amac": (
            "En yeni Sentinel sahnesinde kalite nedeniyle görünmeyen, tarihsel/yedek sahne ile "
            "eski yüzeyi bilinse bile yeni hafriyat başlangıcını saklayabilecek Gülbahçe kör "
            "ceplerinin mevcut insan kör-alan devriyesiyle örtüşmesini ölçmek."
        ),
        "kaynak_son_tarih": source_date or None,
        "kaynak_son_item": source_item or None,
        "coverage_son_tarih": coverage_date or None,
        "coverage_son_item": coverage_item or None,
        "ayni_sentinel_sahnesi": same_scene,
        "guncel_sahne_kor_250_6500": len(covered),
        "secilen_devriye_eslesen_kor_kume": matched_count,
        "secilen_devriye_guncel_korlugu_kapsiyor": matched_count > 0,
        "guncel_sahne_kor_kumeleri": covered,
        "yorum": (
            "Bu sonuç alarm/görev değildir. Yedek sahne eski yüzey bilgisini geri kazansa da "
            "en yeni sahne geçersizse yeni kazının mevcut durumu doğrudan görülemez. Veri tarihleri "
            "uyuşmuyorsa rota kararı verilmez; yalnız diagnostik bekletilir."
        ),
    }


def _self_check():
    micro = {
        "kaynak_son_tarih": "05.09.2026",
        "kaynak_son_item": "S2_TEST",
        "kor_kume_ornekleri": [
            {"enlem": 38.3328, "boylam": 26.6456, "alan_m2": 400, "neden": "BULUT"},
            {"enlem": 38.3400, "boylam": 26.6500, "alan_m2": 1200, "neden": "GOLGE"},
            {"enlem": 38.3310, "boylam": 26.6460, "alan_m2": 200, "neden": "MIKRO"},
        ],
    }
    coverage = {"bolgeler": {"uzunkuyu": {"son_tarih": "05.09.2026", "son_item": "S2_TEST"}}}
    report = {
        "gulbahce_kor_alan_devriye_korumasi": {
            "secilen": {"enlem": 38.3328, "boylam": 26.6456}
        }
    }
    result = build_review(micro, coverage, report)
    assert result["durum"] == "ok"
    assert result["guncel_sahne_kor_250_6500"] == 2
    assert result["secilen_devriye_eslesen_kor_kume"] == 1
    assert result["secilen_devriye_guncel_korlugu_kapsiyor"] is True
    assert all(not row["alarm"] and not row["saha_gorevi"] for row in result["guncel_sahne_kor_kumeleri"])

    stale = build_review(micro, {"bolgeler": {"uzunkuyu": {"son_tarih": "03.09.2026", "son_item": "OLD"}}}, report)
    assert stale["durum"] == "veri_tarihi_uyusmuyor"
    assert stale["ayni_sentinel_sahnesi"] is False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Gülbahçe güncel-sahne körlük öz testi başarılı; 250 m² eşik değişmedi.")
        return

    for path in (MICRO_BLIND_JSON, COVERAGE_JSON, REPORT_JSON):
        if not path.exists():
            raise RuntimeError(f"Gerekli girdi bulunamadı: {path.name}")

    micro = json.loads(MICRO_BLIND_JSON.read_text(encoding="utf-8"))
    coverage = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))
    report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    result = build_review(micro, coverage, report)
    OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "Gülbahçe güncel-sahne körlük: "
        f"durum={result['durum']}, "
        f"250-6500={result['guncel_sahne_kor_250_6500']}, "
        f"devriye-eslesme={result['secilen_devriye_eslesen_kor_kume']}. "
        "Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
