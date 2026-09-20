"""Temel/kepçe pozitif kanıtında yalnız 15–17 Eylül onset penceresini temporal destek sayar.

Bu katman yalnız diagnostik pozitif-kanıt çıktısını daraltır; yeni aday, alarm veya saha
görevi üretmez. 15 Eylül öncesi temporal-lokal izler sakin baseline bağlamıdır ve 18
Eylül morfoloji adayına bağımsız pozitif destek olarak sayılamaz. 15, 16 veya 17 Eylül
izi varsa ve üst katman onu mekânsal/zamansal olarak eşleştirmişse destek korunur.

SAR pozitif desteği bağımsız kalır. Bu nedenle eski bir baseline temporal izi olan fakat
aynı koordinatta güçlü Sentinel-1 desteği taşıyan aday yalnız temporal kanıtını kaybeder;
SAR nedeniyle çoklu-pozitif statüsü korunabilir. 250 m² ana eşik ile 150–249 m² MİKRO
politikası değişmez.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path


BASE = Path(__file__).resolve().parent
INPUT_JSON = BASE / "foundation_excavation_positive_evidence_review.json"
OUTPUT_JSON = INPUT_JSON

ONSET_START = date(2026, 9, 15)
ONSET_END = date(2026, 9, 17)
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]


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
            continue
    return None


def _area(item):
    try:
        return int(item.get("spektral_etki_alani_m2") or 0)
    except (TypeError, ValueError):
        return 0


def _guard_row(candidate):
    row = dict(candidate)
    morphology_ok = candidate.get("morfoloji_yuksek") is True
    sar_positive = candidate.get("sar_pozitif_destek") is True
    original_temporal = candidate.get("temporal_oncul_destek") is True
    temporal_date = _date(candidate.get("temporal_oncul_tarih"))
    temporal_in_onset = bool(
        original_temporal
        and temporal_date is not None
        and ONSET_START <= temporal_date <= ONSET_END
    )
    blocked = candidate.get("negatif_baglamsal_engel") is True

    independent_positive = bool(sar_positive or temporal_in_onset)
    multi_positive = bool(morphology_ok and independent_positive and not blocked)
    area_m2 = _area(candidate)
    main_band = area_m2 >= MAIN_THRESHOLD_M2
    micro_band = MICRO_RANGE_M2[0] <= area_m2 <= MICRO_RANGE_M2[1]

    sources = []
    if morphology_ok:
        sources.append("S2_MORFOLOJI")
    if sar_positive:
        sources.append("S1_LOKAL_POZITIF")
    if temporal_in_onset:
        sources.append("S2_ONSET_TEMPORAL")

    row["temporal_oncul_destek_ham"] = original_temporal
    row["temporal_onset_penceresi_gecerli"] = temporal_in_onset
    row["temporal_oncul_baseline_olarak_haric"] = bool(
        original_temporal
        and temporal_date is not None
        and temporal_date < ONSET_START
    )
    row["temporal_oncul_destek"] = temporal_in_onset
    row["pozitif_kanit_kaynaklari"] = sources
    row["bagimsiz_pozitif_destek"] = independent_positive
    row["coklu_pozitif_kanit"] = multi_positive
    row["ana_esik"] = main_band
    row["ana_esik_pozitif_destekli_diagnostik"] = bool(multi_positive and main_band)
    row["mikro_pozitif_destekli_diagnostik"] = bool(multi_positive and micro_band)
    row["uc_eksenli_destek"] = bool(
        morphology_ok and sar_positive and temporal_in_onset and not blocked
    )
    row["alarm"] = False
    row["saha_gorevi"] = False
    return row


def _guard_region(region):
    rows = [
        _guard_row(item)
        for item in (region.get("adaylar") or [])
        if isinstance(item, dict)
    ]
    rows.sort(
        key=lambda item: (
            bool(item.get("ana_esik_pozitif_destekli_diagnostik")),
            bool(item.get("uc_eksenli_destek")),
            bool(item.get("coklu_pozitif_kanit")),
            -bool(item.get("negatif_baglamsal_engel")),
            float(item.get("morfoloji_puani") or 0),
        ),
        reverse=True,
    )
    result = dict(region)
    result["adaylar"] = rows
    result["ana_esik_pozitif_destekli_sayi"] = sum(
        1 for item in rows if item.get("ana_esik_pozitif_destekli_diagnostik") is True
    )
    result["mikro_pozitif_destekli_sayi"] = sum(
        1 for item in rows if item.get("mikro_pozitif_destekli_diagnostik") is True
    )
    result["uc_eksenli_destekli_sayi"] = sum(
        1 for item in rows if item.get("uc_eksenli_destek") is True
    )
    result["onset_penceresi_temporal_destekli_sayi"] = sum(
        1 for item in rows if item.get("temporal_onset_penceresi_gecerli") is True
    )
    result["baseline_temporal_destegi_haric_sayi"] = sum(
        1 for item in rows if item.get("temporal_oncul_baseline_olarak_haric") is True
    )
    return result


def guard(payload):
    if not isinstance(payload, dict):
        raise RuntimeError("Pozitif kanıt diagnostik çıktısı bulunamadı.")

    regions = {}
    for region_key in ("cesme", "uzunkuyu"):
        regions[region_key] = _guard_region(
            (payload.get("bolgeler") or {}).get(region_key) or {}
        )

    result = dict(payload)
    result["surum"] = max(int(result.get("surum") or 0), 4)
    result["bolgeler"] = regions
    result["onset_temporal_koruma_uygulandi"] = True
    result["onset_temporal_penceresi"] = [
        ONSET_START.isoformat(),
        ONSET_END.isoformat(),
    ]
    result["pre_onset_temporal_pozitif_destek_sayilir"] = False
    result["toplam_ana_esik_pozitif_destekli_diagnostik"] = sum(
        region.get("ana_esik_pozitif_destekli_sayi", 0) for region in regions.values()
    )
    result["toplam_mikro_pozitif_destekli_diagnostik"] = sum(
        region.get("mikro_pozitif_destekli_sayi", 0) for region in regions.values()
    )
    result["toplam_uc_eksenli_destekli_diagnostik"] = sum(
        region.get("uc_eksenli_destekli_sayi", 0) for region in regions.values()
    )
    result["toplam_onset_penceresi_temporal_destekli"] = sum(
        region.get("onset_penceresi_temporal_destekli_sayi", 0) for region in regions.values()
    )
    result["toplam_baseline_temporal_destegi_haric"] = sum(
        region.get("baseline_temporal_destegi_haric_sayi", 0) for region in regions.values()
    )
    result["not"] = (
        "Pozitif temporal ikinci kanıt yalnız 15–17 Eylül 2026 onset penceresinde kabul edilir. "
        "13 Eylül ve daha eski temporal-lokal izler sakin baseline kabul edilir ve yeni kazı "
        "kanıtı olarak sayılmaz. Sentinel-1 pozitif desteği bağımsız kalır; SAR yokluğu veto "
        "değildir. 250 m² ana ve 150–249 m² MİKRO eşikleri değişmez."
    )
    return result


def _self_check():
    payload = {
        "surum": 3,
        "bolgeler": {
            "cesme": {
                "adaylar": [
                    {
                        "enlem": 38.30,
                        "boylam": 26.30,
                        "spektral_etki_alani_m2": 400,
                        "morfoloji_puani": 90,
                        "morfoloji_yuksek": True,
                        "sar_pozitif_destek": False,
                        "temporal_oncul_destek": True,
                        "temporal_oncul_tarih": "13.09.2026",
                        "negatif_baglamsal_engel": False,
                    },
                    {
                        "enlem": 38.31,
                        "boylam": 26.31,
                        "spektral_etki_alani_m2": 400,
                        "morfoloji_puani": 90,
                        "morfoloji_yuksek": True,
                        "sar_pozitif_destek": False,
                        "temporal_oncul_destek": True,
                        "temporal_oncul_tarih": "15.09.2026",
                        "negatif_baglamsal_engel": False,
                    },
                    {
                        "enlem": 38.32,
                        "boylam": 26.32,
                        "spektral_etki_alani_m2": 200,
                        "morfoloji_puani": 95,
                        "morfoloji_yuksek": True,
                        "sar_pozitif_destek": True,
                        "temporal_oncul_destek": True,
                        "temporal_oncul_tarih": "08.09.2026",
                        "negatif_baglamsal_engel": False,
                    },
                    {
                        "enlem": 38.33,
                        "boylam": 26.33,
                        "spektral_etki_alani_m2": 500,
                        "morfoloji_puani": 95,
                        "morfoloji_yuksek": True,
                        "sar_pozitif_destek": False,
                        "temporal_oncul_destek": True,
                        "temporal_oncul_tarih": "16.09.2026",
                        "negatif_baglamsal_engel": True,
                    },
                ]
            },
            "uzunkuyu": {"adaylar": []},
        },
    }
    guarded = guard(payload)
    rows = guarded["bolgeler"]["cesme"]["adaylar"]
    by_lat = {round(float(item["enlem"]), 2): item for item in rows}

    baseline = by_lat[38.30]
    assert baseline["temporal_oncul_destek_ham"] is True
    assert baseline["temporal_oncul_baseline_olarak_haric"] is True
    assert baseline["temporal_oncul_destek"] is False
    assert baseline["coklu_pozitif_kanit"] is False
    assert baseline["ana_esik_pozitif_destekli_diagnostik"] is False

    onset = by_lat[38.31]
    assert onset["temporal_onset_penceresi_gecerli"] is True
    assert onset["coklu_pozitif_kanit"] is True
    assert onset["ana_esik_pozitif_destekli_diagnostik"] is True
    assert "S2_ONSET_TEMPORAL" in onset["pozitif_kanit_kaynaklari"]

    sar = by_lat[38.32]
    assert sar["temporal_oncul_destek"] is False
    assert sar["sar_pozitif_destek"] is True
    assert sar["coklu_pozitif_kanit"] is True
    assert sar["mikro_pozitif_destekli_diagnostik"] is True
    assert sar["uc_eksenli_destek"] is False

    blocked = by_lat[38.33]
    assert blocked["temporal_oncul_destek"] is True
    assert blocked["coklu_pozitif_kanit"] is False

    assert guarded["toplam_ana_esik_pozitif_destekli_diagnostik"] == 1
    assert guarded["toplam_mikro_pozitif_destekli_diagnostik"] == 1
    assert guarded["toplam_baseline_temporal_destegi_haric"] == 2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("foundation excavation onset temporal guard self-check: ok")
        return 0

    payload = guard(_load(INPUT_JSON))
    OUTPUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
