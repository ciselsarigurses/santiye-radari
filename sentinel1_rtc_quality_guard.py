"""Sentinel-1 RTC diagnostiginin veri tazeligi ve okunabilirlik kalitesini ozetler.

Bu katman alarm veya saha gorevi uretmez. RTC lokal degisim analizinin geometrik
kapsama ile gercekten metrik uretilebilen hedefleri birbirine karistirmamasini
saglar. Ozellikle tile kenari/nodata nedeniyle YETERSIZ_GECERLI_PIKSEL kalan
hedefleri ve kullanilan RTC ciftinin yasini acikca raporlar.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path


FRESH_DAYS = 2
AGING_DAYS = 5


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def _freshness(value, today=None):
    scene_date = _parse_date(value)
    if scene_date is None:
        return "RTC_TARIHI_YOK", None
    today = today or datetime.now(timezone.utc).date()
    age = max(0, (today - scene_date).days)
    if age <= FRESH_DAYS:
        status = "RTC_GUNCEL"
    elif age <= AGING_DAYS:
        status = "RTC_GECIKMELI"
    else:
        status = "RTC_ESKIMIS"
    return status, age


def _has_error(target, code):
    errors = target.get("okuma_hatalari") or {}
    for pol_row in errors.values():
        if not isinstance(pol_row, dict):
            continue
        if code in (pol_row.get("eski"), pol_row.get("yeni")):
            return True
    return False


def _has_other_read_error(target):
    errors = target.get("okuma_hatalari") or {}
    for pol_row in errors.values():
        if not isinstance(pol_row, dict):
            continue
        for value in (pol_row.get("eski"), pol_row.get("yeni")):
            if value and value != "YETERSIZ_GECERLI_PIKSEL":
                return True
    return False


def region_quality(row, today=None):
    targets = row.get("hedefler") or []
    covered = [target for target in targets if target.get("rtc_cifti_kapsiyor")]
    with_metric = [
        target
        for target in covered
        if target.get("sar_lokal_degisim_skor_db") is not None
    ]
    no_metric = [
        target
        for target in covered
        if target.get("sar_lokal_degisim_skor_db") is None
    ]
    invalid_pixel = [target for target in covered if _has_error(target, "YETERSIZ_GECERLI_PIKSEL")]
    other_error = [target for target in covered if _has_other_read_error(target)]

    freshness, age_days = _freshness(row.get("yeni_tarih"), today=today)
    ratio = round(len(with_metric) / len(covered), 3) if covered else None

    issues = []
    if no_metric:
        issues.append("GEOMETRIK_KAPSAMA_VAR_METRIK_YOK")
    if invalid_pixel:
        issues.append("RTC_GECERLI_PIKSEL_BOSLUGU")
    if other_error:
        issues.append("RTC_OKUMA_HATASI")
    if freshness in ("RTC_GECIKMELI", "RTC_ESKIMIS"):
        issues.append(freshness)

    if not covered:
        status = "RTC_HEDEF_KAPSAMASI_YOK"
    elif not with_metric:
        status = "RTC_METRIK_OKUNAMADI"
    elif freshness == "RTC_ESKIMIS":
        status = "RTC_DIAGNOSTIK_ESKI_CIFT"
    elif ratio is not None and ratio < 0.8:
        status = "RTC_DIAGNOSTIK_KISMEN_OKUNABILIR"
    elif freshness == "RTC_GECIKMELI":
        status = "RTC_DIAGNOSTIK_GECIKMELI"
    else:
        status = "RTC_DIAGNOSTIK_KALITE_UYGUN"

    return {
        "bolge": row.get("bolge"),
        "durum": status,
        "rtc_yeni_tarih": row.get("yeni_tarih"),
        "rtc_cifti_yasi_gun": age_days,
        "rtc_tazelik": freshness,
        "hedef_sayisi": len(targets),
        "geometrik_kapsanan_hedef": len(covered),
        "metrik_uretilen_hedef": len(with_metric),
        "metriksiz_kapsanan_hedef": len(no_metric),
        "yetersiz_gecerli_piksel_hedef": len(invalid_pixel),
        "diger_okuma_hatasi_hedef": len(other_error),
        "gecerli_metrik_orani": ratio,
        "dikkat": issues,
        "alarm": False,
        "saha_gorevi": False,
    }


def evaluate(payload, today=None):
    rows = [region_quality(row, today=today) for row in (payload.get("bolgeler") or [])]
    return {
        "alarm": False,
        "saha_gorevi": False,
        "amac": "RTC geometrik kapsama, gercek piksel okunabilirligi ve sahne tazeligini ayri kalite kapilari olarak izlemek; tek basina insaat/kazi sonucu cikarmamak.",
        "bolgeler": rows,
        "dikkat_bolgeleri": [row["bolge"] for row in rows if row["dikkat"]],
        "toplam_geometrik_kapsanan_hedef": sum(row["geometrik_kapsanan_hedef"] for row in rows),
        "toplam_metrik_uretilen_hedef": sum(row["metrik_uretilen_hedef"] for row in rows),
        "toplam_yetersiz_gecerli_piksel_hedef": sum(row["yetersiz_gecerli_piksel_hedef"] for row in rows),
    }


def _self_check():
    payload = {
        "bolgeler": [
            {
                "bolge": "gulbahce",
                "yeni_tarih": "2026-09-06",
                "hedefler": [
                    {"rtc_cifti_kapsiyor": True, "sar_lokal_degisim_skor_db": 0.7},
                    {
                        "rtc_cifti_kapsiyor": True,
                        "okuma_hatalari": {
                            "VV": {"eski": "YETERSIZ_GECERLI_PIKSEL", "yeni": None}
                        },
                    },
                    {"rtc_cifti_kapsiyor": True, "sar_lokal_degisim_skor_db": 0.2},
                ],
            }
        ]
    }
    result = evaluate(payload, today=date(2026, 9, 12))
    row = result["bolgeler"][0]
    assert row["rtc_cifti_yasi_gun"] == 6, row
    assert row["rtc_tazelik"] == "RTC_ESKIMIS", row
    assert row["gecerli_metrik_orani"] == 0.667, row
    assert row["yetersiz_gecerli_piksel_hedef"] == 1, row
    assert row["durum"] == "RTC_DIAGNOSTIK_ESKI_CIFT", row
    assert result["alarm"] is False and result["saha_gorevi"] is False
    print("Sentinel-1 RTC veri kalite kapisi oz testi OK.")


def _read_input(path):
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check-only", action="store_true")
    parser.add_argument("--input", default="-")
    args = parser.parse_args()

    if args.self_check_only:
        _self_check()
        return

    payload = _read_input(args.input)
    print(json.dumps(evaluate(payload), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
