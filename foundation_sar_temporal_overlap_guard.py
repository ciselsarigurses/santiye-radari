"""Doğrudan S2-SAR temel/kepçe desteğinde gözlem penceresi kesişimini doğrular.

Sentinel-1'in yalnız S2 başlangıç tarihinden sonra gelmesi yeterli değildir. Bir S1
çifti, eski bir S2 değişimini doğrudan çapraz kanıt sayabilmek için S2 değişim
penceresiyle zamansal olarak kesişmelidir. Örneğin S2 15->18 Eylül ile S1 17->23
Eylül kesişir; S1 23->29 Eylül ise kesişmez ve ancak ayrı bir devam/persistence
katmanında kullanılabilir.

Bu dosya üretim skoru, rota veya aday üretmez. Mevcut
``foundation_excavation_sar_seed_review.json`` çıktısındaki doğrudan çapraz destek
sayısı, satır işaretleri ve tarih semantiğinin tutarlı kalmasını fail-closed denetler.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path


BASE = Path(__file__).resolve().parent
INPUT_JSON = BASE / "foundation_excavation_sar_seed_review.json"
REGIONS = ("cesme", "uzunkuyu")


def _date(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _windows_overlap(a_start, a_end, b_start, b_end):
    dates = [_date(a_start), _date(a_end), _date(b_start), _date(b_end)]
    if any(value is None for value in dates):
        return None
    a0, a1, b0, b1 = dates
    if a0 > a1 or b0 > b1:
        return False
    return max(a0, b0) <= min(a1, b1)


def _load(path=INPUT_JSON):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def audit(payload=None):
    payload = payload if isinstance(payload, dict) else _load()
    rows = {}
    mismatches = []

    for region_key in REGIONS:
        region = (payload.get("bolgeler") or {}).get(region_key) or {}
        try:
            cross_count = int(region.get("s2_sar_capraz_destekli_sayi") or 0)
        except (TypeError, ValueError):
            cross_count = 0

        cross_rows_raw = region.get("capraz_destekli_adaylar")
        cross_rows = cross_rows_raw if isinstance(cross_rows_raw, list) else []
        row_count = len(cross_rows)
        count_mismatch = cross_count != row_count
        invalid_row_indexes = []
        for index, row in enumerate(cross_rows):
            if not isinstance(row, dict):
                invalid_row_indexes.append(index)
                continue
            if row.get("s2_sar_capraz_destek") is not True:
                invalid_row_indexes.append(index)
                continue
            if row.get("sar_optik_doneme_zamansal_uygun") is not True:
                invalid_row_indexes.append(index)

        overlap = _windows_overlap(
            region.get("s2_onceki_tarih"),
            region.get("s2_son_tarih"),
            region.get("sar_eski_tarih"),
            region.get("sar_yeni_tarih"),
        )
        temporal_mismatch = bool(cross_count > 0 and overlap is not True)
        row_mismatch = bool(count_mismatch or invalid_row_indexes)
        mismatch = bool(temporal_mismatch or row_mismatch)
        if mismatch:
            mismatches.append(region_key)

        rows[region_key] = {
            "s2_onceki_tarih": region.get("s2_onceki_tarih"),
            "s2_son_tarih": region.get("s2_son_tarih"),
            "sar_eski_tarih": region.get("sar_eski_tarih"),
            "sar_yeni_tarih": region.get("sar_yeni_tarih"),
            "s2_sar_capraz_destekli_sayi": cross_count,
            "capraz_destekli_satir_sayisi": row_count,
            "capraz_destek_sayi_satir_tutarli": not count_mismatch,
            "gecersiz_capraz_destek_satir_indeksleri": invalid_row_indexes,
            "capraz_destek_satirlari_tutarli": not invalid_row_indexes,
            "gozlem_pencereleri_kesisiyor": overlap,
            "dogrudan_capraz_destek_temporal_uyumlu": not temporal_mismatch,
            "dogrudan_capraz_destek_tutarli": not mismatch,
        }

    return {
        "surum": 2,
        "amac": "Doğrudan S2-SAR temel/kepçe çapraz desteğini kesişen gözlem pencerelerine ve tutarlı aday satırlarına sabitlemek",
        "uretim_filtresi": False,
        "alarm": False,
        "saha_gorevi": False,
        "ciddi_temporal_uyumsuzluk": bool(mismatches),
        "uyumsuz_bolgeler": mismatches,
        "bolgeler": rows,
        "not": (
            "S2-SAR doğrudan çapraz destek yalnız S1 ve S2 gözlem aralıkları en az bir gün "
            "kesişiyorsa geçerlidir. Ayrıca bölgesel çapraz-destek sayısı capraz_destekli_adaylar "
            "satır sayısıyla eşleşmeli ve her satır açıkça zamansal uygun + çapraz destekli olmalıdır. "
            "Kesişmeyen daha yeni SAR değişimleri doğrudan onset kanıtı değil, ayrı devam/persistence "
            "diagnostigidir. Bu koruma skor veya rota üretmez."
        ),
    }


def _self_check():
    assert _date("18.09.2026") == _date("2026-09-18")
    assert _windows_overlap("15.09.2026", "18.09.2026", "17.09.2026", "23.09.2026") is True
    assert _windows_overlap("15.09.2026", "18.09.2026", "11.09.2026", "17.09.2026") is True
    assert _windows_overlap("15.09.2026", "18.09.2026", "23.09.2026", "29.09.2026") is False
    assert _windows_overlap("15.09.2026", None, "17.09.2026", "23.09.2026") is None

    good_row = {
        "s2_sar_capraz_destek": True,
        "sar_optik_doneme_zamansal_uygun": True,
    }
    non_overlap_with_cross = {
        "bolgeler": {
            "cesme": {
                "s2_onceki_tarih": "15.09.2026",
                "s2_son_tarih": "18.09.2026",
                "sar_eski_tarih": "23.09.2026",
                "sar_yeni_tarih": "29.09.2026",
                "s2_sar_capraz_destekli_sayi": 2,
                "capraz_destekli_adaylar": [good_row, good_row],
            },
            "uzunkuyu": {},
        }
    }
    result = audit(non_overlap_with_cross)
    assert result["ciddi_temporal_uyumsuzluk"] is True
    assert result["uyumsuz_bolgeler"] == ["cesme"]

    non_overlap_without_cross = {
        "bolgeler": {
            "cesme": {
                "s2_onceki_tarih": "15.09.2026",
                "s2_son_tarih": "18.09.2026",
                "sar_eski_tarih": "23.09.2026",
                "sar_yeni_tarih": "29.09.2026",
                "s2_sar_capraz_destekli_sayi": 0,
                "capraz_destekli_adaylar": [],
            },
            "uzunkuyu": {},
        }
    }
    assert audit(non_overlap_without_cross)["ciddi_temporal_uyumsuzluk"] is False

    count_drift = {
        "bolgeler": {
            "cesme": {
                "s2_onceki_tarih": "15.09.2026",
                "s2_son_tarih": "18.09.2026",
                "sar_eski_tarih": "17.09.2026",
                "sar_yeni_tarih": "23.09.2026",
                "s2_sar_capraz_destekli_sayi": 2,
                "capraz_destekli_adaylar": [good_row],
            },
            "uzunkuyu": {},
        }
    }
    result = audit(count_drift)
    assert result["ciddi_temporal_uyumsuzluk"] is True
    assert result["bolgeler"]["cesme"]["capraz_destek_sayi_satir_tutarli"] is False

    bad_row_flag = {
        "bolgeler": {
            "cesme": {
                "s2_onceki_tarih": "15.09.2026",
                "s2_son_tarih": "18.09.2026",
                "sar_eski_tarih": "17.09.2026",
                "sar_yeni_tarih": "23.09.2026",
                "s2_sar_capraz_destekli_sayi": 1,
                "capraz_destekli_adaylar": [
                    {
                        "s2_sar_capraz_destek": True,
                        "sar_optik_doneme_zamansal_uygun": False,
                    }
                ],
            },
            "uzunkuyu": {},
        }
    }
    result = audit(bad_row_flag)
    assert result["ciddi_temporal_uyumsuzluk"] is True
    assert result["bolgeler"]["cesme"]["capraz_destek_satirlari_tutarli"] is False

    fully_consistent = {
        "bolgeler": {
            "cesme": {
                "s2_onceki_tarih": "15.09.2026",
                "s2_son_tarih": "18.09.2026",
                "sar_eski_tarih": "17.09.2026",
                "sar_yeni_tarih": "23.09.2026",
                "s2_sar_capraz_destekli_sayi": 1,
                "capraz_destekli_adaylar": [good_row],
            },
            "uzunkuyu": {},
        }
    }
    assert audit(fully_consistent)["ciddi_temporal_uyumsuzluk"] is False


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args(argv)

    _self_check()
    if args.check_only:
        print("foundation S2-SAR temporal overlap self-check: ok")
        return

    result = audit()
    print(json.dumps(result, ensure_ascii=False))
    if args.enforce and result["ciddi_temporal_uyumsuzluk"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
