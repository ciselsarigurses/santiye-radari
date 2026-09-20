"""15–17 Eylül başlangıç-penceresi Sentinel-1 desteğini pozitif kanıt zincirine bağlar.

Bu katman yalnız mevcut S2 morfoloji adayına bağımsız POZİTİF kanıt ekler; yeni
aday üretmez ve zayıf SAR'ı negatif veto yapmaz. Kaynak, aynı-geometrili
11→17 Eylül Sentinel-1 RTC diagnostigidir. Köprü fail-closed çalışır:

- onset SAR regresyon setinin tamamı ölçülmüş olmalı,
- kullanıcının dört çekirdek yanlış-pozitifi bu çiftte güçlü SAR vermemiş olmalı,
- bölgenin exact 11→17 çifti mevcut olmalı,
- aday mevcut pozitif-evidence havuzunda yüksek S2 morfolojili olmalı,
- aynı koordinatta (<=15 m) kompakt, çift-pol güçlü ve >=2 dB onset SAR olmalı,
- saha/ müşteri / tarla-bahçe / çekirdek FP-klon negatiflerinden hiçbiri olmamalı.

Doğrulanmış gerçek kazının onset SAR'da zayıf kalması köprüyü kapatmaz; çünkü SAR
burada yalnız pozitif destek yoludur, hard-veto değildir. 250 m² ana eşik korunur,
150–249 m² MİKRO yalnız diagnostik kalır. Sentinel-2'den gerçek derinlik ölçülmez.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


BASE = Path(__file__).resolve().parent
POSITIVE_JSON = BASE / "foundation_excavation_positive_evidence_review.json"
ONSET_SAR_JSON = BASE / "foundation_excavation_onset_sar_review.json"
OUTPUT_JSON = POSITIVE_JSON

MATCH_RADIUS_M = 15.0
MIN_ONSET_SAR_DB = 2.0
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
EXPECTED_ONSET_PAIR = "2026-09-11->2026-09-17"
CORE_FP_IDS = {
    "FP-20260916-REISDERE-001",
    "FP-20260917-UZUNKUYU-001",
    "FP-20260917-MUSALLA-001",
    "FP-20260917-MUSALLA-002",
}


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
    best = None
    best_distance = None
    for row in records:
        row_point = _point(row)
        if row_point is None:
            continue
        distance = _distance_m(target_point, row_point)
        if best_distance is None or distance < best_distance:
            best = row
            best_distance = distance
    return best, best_distance


def _area(item):
    try:
        return int(item.get("spektral_etki_alani_m2") or 0)
    except (TypeError, ValueError):
        return 0


def _core_fp_precision_pass(onset):
    if onset.get("kritik_regresyon_tamam") is not True:
        return False
    rows = {
        str(row.get("id")): row
        for row in (onset.get("kritik_regresyon") or [])
        if isinstance(row, dict) and row.get("id")
    }
    if not CORE_FP_IDS.issubset(rows):
        return False
    return all(
        rows[item_id].get("olculdu") is True
        and rows[item_id].get("onset_sar_guclu_lokal_destek") is False
        for item_id in CORE_FP_IDS
    )


def _strong_onset_rows(region):
    if region.get("tam_onset_cifti") is not True:
        return []
    if str(region.get("istenen_sar_cifti") or "") != EXPECTED_ONSET_PAIR:
        return []
    rows = []
    for row in (region.get("onset_ana_destekli_adaylar") or []) + (
        region.get("onset_mikro_destekli_adaylar") or []
    ):
        if not isinstance(row, dict) or _point(row) is None:
            continue
        try:
            score = float(row.get("sar_lokal_degisim_skor_db"))
        except (TypeError, ValueError):
            continue
        if (
            row.get("onset_sar_guclu_lokal_destek") is True
            and row.get("geri_bildirim_engeli") is not True
            and score >= MIN_ONSET_SAR_DB
            and str(row.get("sar_polarizasyon_uyumu") or "") == "CIFT_POL_GUCLU"
            and str(row.get("sar_mekansal_ayrim") or "") == "KOMPAKT_LOKAL_DESTEKLI"
        ):
            rows.append(row)
    return rows


def _blocked(candidate):
    return bool(
        candidate.get("negatif_baglamsal_engel") is True
        or candidate.get("geri_bildirim_engeli") is True
        or candidate.get("mevcut_musteri") is True
        or candidate.get("tarla_bahce_baskilandi") is True
        or candidate.get("yanlis_pozitif_spektral_klon_baskilandi") is True
    )


def _append_source(existing, source):
    values = [str(x) for x in (existing or []) if str(x).strip()]
    if source not in values:
        values.append(source)
    return values


def _bridge_row(candidate, onset_rows, bridge_enabled):
    row = dict(candidate)
    match, distance = _nearest(candidate, onset_rows) if bridge_enabled else (None, None)
    matched = bool(
        match is not None
        and distance is not None
        and distance <= MATCH_RADIUS_M
        and candidate.get("morfoloji_yuksek") is True
        and not _blocked(candidate)
    )

    row["onset_sar_kopru_uygulandi"] = bridge_enabled
    row["onset_sar_pozitif_destek"] = matched
    row["onset_sar_eslesme_mesafe_m"] = round(distance, 1) if matched else None
    row["onset_sar_cifti"] = EXPECTED_ONSET_PAIR if matched else None
    row["onset_sar_lokal_degisim_skor_db"] = (
        match.get("sar_lokal_degisim_skor_db") if matched else None
    )
    row["onset_sar_polarizasyon_uyumu"] = (
        match.get("sar_polarizasyon_uyumu") if matched else None
    )
    row["onset_sar_mekansal_ayrim"] = (
        match.get("sar_mekansal_ayrim") if matched else None
    )

    # SAR burada yalnız pozitif destek: mevcut latest-SAR veya 15–17 temporal
    # kanıtı aynen korunur; zayıf/onset-eşleşmesiz SAR asla negatif veto değildir.
    existing_independent = bool(
        candidate.get("sar_pozitif_destek") is True
        or candidate.get("temporal_oncul_destek") is True
    )
    independent = bool(existing_independent or matched)
    morphology_ok = candidate.get("morfoloji_yuksek") is True
    blocked = _blocked(candidate)
    multi = bool(morphology_ok and independent and not blocked)
    area_m2 = _area(candidate)
    main_band = area_m2 >= MAIN_THRESHOLD_M2
    micro_band = MICRO_RANGE_M2[0] <= area_m2 <= MICRO_RANGE_M2[1]

    sources = candidate.get("pozitif_kanit_kaynaklari") or []
    if matched:
        sources = _append_source(sources, "S1_ONSET_11_17")
    row["pozitif_kanit_kaynaklari"] = sources
    row["bagimsiz_pozitif_destek"] = independent
    row["coklu_pozitif_kanit"] = multi
    row["ana_esik"] = main_band
    row["ana_esik_pozitif_destekli_diagnostik"] = bool(multi and main_band)
    row["mikro_pozitif_destekli_diagnostik"] = bool(multi and micro_band)
    row["sar_yoklugu_negatif_kanit"] = False
    row["alarm"] = False
    row["saha_gorevi"] = False
    return row


def _bridge_region(region_key, positive_region, onset_region, bridge_enabled):
    onset_rows = _strong_onset_rows(onset_region) if bridge_enabled else []
    rows = [
        _bridge_row(candidate, onset_rows, bridge_enabled)
        for candidate in (positive_region.get("adaylar") or [])
        if isinstance(candidate, dict)
    ]
    rows.sort(
        key=lambda item: (
            bool(item.get("ana_esik_pozitif_destekli_diagnostik")),
            bool(item.get("mikro_pozitif_destekli_diagnostik")),
            bool(item.get("onset_sar_pozitif_destek")),
            bool(item.get("coklu_pozitif_kanit")),
            float(item.get("morfoloji_puani") or 0),
        ),
        reverse=True,
    )
    result = dict(positive_region)
    result["adaylar"] = rows
    result["onset_sar_kopru_aktif"] = bridge_enabled
    result["onset_sar_guclu_hedef_sayisi"] = len(onset_rows)
    result["onset_sar_eslesen_sayi"] = sum(
        1 for row in rows if row.get("onset_sar_pozitif_destek") is True
    )
    result["ana_esik_pozitif_destekli_sayi"] = sum(
        1 for row in rows if row.get("ana_esik_pozitif_destekli_diagnostik") is True
    )
    result["mikro_pozitif_destekli_sayi"] = sum(
        1 for row in rows if row.get("mikro_pozitif_destekli_diagnostik") is True
    )
    return result


def bridge(positive, onset):
    if not isinstance(positive, dict):
        raise RuntimeError("Pozitif kanıt çıktısı bulunamadı.")
    onset = onset if isinstance(onset, dict) else {}
    precision_pass = _core_fp_precision_pass(onset)

    regions = {}
    for region_key in ("cesme", "uzunkuyu"):
        regions[region_key] = _bridge_region(
            region_key,
            (positive.get("bolgeler") or {}).get(region_key) or {},
            (onset.get("bolgeler") or {}).get(region_key) or {},
            precision_pass,
        )

    result = dict(positive)
    result["surum"] = max(int(result.get("surum") or 0), 5)
    result["bolgeler"] = regions
    result["onset_sar_pozitif_kopru_uygulandi"] = True
    result["onset_sar_pozitif_kopru_aktif"] = precision_pass
    result["onset_sar_kopru_eslesme_yaricapi_m"] = MATCH_RADIUS_M
    result["onset_sar_kopru_min_db"] = MIN_ONSET_SAR_DB
    result["onset_sar_kopru_cifti"] = EXPECTED_ONSET_PAIR
    result["onset_sar_core_fp_precision_geciyor"] = precision_pass
    result["sar_yoklugu_negatif_kanit"] = False
    result["toplam_onset_sar_pozitif_destekli"] = sum(
        region.get("onset_sar_eslesen_sayi", 0) for region in regions.values()
    )
    result["toplam_ana_esik_pozitif_destekli_diagnostik"] = sum(
        region.get("ana_esik_pozitif_destekli_sayi", 0) for region in regions.values()
    )
    result["toplam_mikro_pozitif_destekli_diagnostik"] = sum(
        region.get("mikro_pozitif_destekli_sayi", 0) for region in regions.values()
    )
    result["not"] = (
        "Exact 11→17 Eylül Sentinel-1 RTC yalnız güçlü, çift-pol ve kompakt lokal "
        "destek verdiğinde mevcut yüksek-S2-morfoloji adayına bağımsız pozitif kanıt "
        "eklenir. Dört çekirdek yanlış-pozitif bu çiftte güçlü değilse köprü açılır. "
        "Doğrulanmış kazının SAR'da zayıf kalması veto değildir. 250 m² ana eşik ve "
        "150–249 m² MİKRO diagnostik politikası değişmez; bu katman alarm/görev üretmez."
    )
    return result


def _self_check():
    base_candidate = {
        "enlem": 38.30,
        "boylam": 26.30,
        "spektral_etki_alani_m2": 400,
        "morfoloji_puani": 95,
        "morfoloji_yuksek": True,
        "sar_pozitif_destek": False,
        "temporal_oncul_destek": False,
        "negatif_baglamsal_engel": False,
        "geri_bildirim_engeli": False,
        "mevcut_musteri": False,
        "tarla_bahce_baskilandi": False,
        "yanlis_pozitif_spektral_klon_baskilandi": False,
        "pozitif_kanit_kaynaklari": ["S2_MORFOLOJI"],
    }
    positive = {
        "surum": 4,
        "bolgeler": {
            "cesme": {"adaylar": [base_candidate]},
            "uzunkuyu": {"adaylar": []},
        },
    }
    regress = [
        {
            "id": item_id,
            "olculdu": True,
            "onset_sar_guclu_lokal_destek": False,
        }
        for item_id in sorted(CORE_FP_IDS)
    ]
    onset = {
        "kritik_regresyon_tamam": True,
        "kritik_regresyon": regress,
        "bolgeler": {
            "cesme": {
                "tam_onset_cifti": True,
                "istenen_sar_cifti": EXPECTED_ONSET_PAIR,
                "onset_ana_destekli_adaylar": [
                    {
                        "enlem": 38.30001,
                        "boylam": 26.30001,
                        "sar_lokal_degisim_skor_db": 2.8,
                        "sar_polarizasyon_uyumu": "CIFT_POL_GUCLU",
                        "sar_mekansal_ayrim": "KOMPAKT_LOKAL_DESTEKLI",
                        "onset_sar_guclu_lokal_destek": True,
                        "geri_bildirim_engeli": False,
                    }
                ],
                "onset_mikro_destekli_adaylar": [],
            },
            "uzunkuyu": {
                "tam_onset_cifti": True,
                "istenen_sar_cifti": EXPECTED_ONSET_PAIR,
                "onset_ana_destekli_adaylar": [],
                "onset_mikro_destekli_adaylar": [],
            },
        },
    }
    payload = bridge(positive, onset)
    row = payload["bolgeler"]["cesme"]["adaylar"][0]
    assert payload["onset_sar_pozitif_kopru_aktif"] is True
    assert row["onset_sar_pozitif_destek"] is True
    assert row["ana_esik_pozitif_destekli_diagnostik"] is True
    assert "S1_ONSET_11_17" in row["pozitif_kanit_kaynaklari"]

    micro = json.loads(json.dumps(positive))
    micro["bolgeler"]["cesme"]["adaylar"][0]["spektral_etki_alani_m2"] = 200
    micro_row = bridge(micro, onset)["bolgeler"]["cesme"]["adaylar"][0]
    assert micro_row["ana_esik_pozitif_destekli_diagnostik"] is False
    assert micro_row["mikro_pozitif_destekli_diagnostik"] is True

    blocked = json.loads(json.dumps(positive))
    blocked["bolgeler"]["cesme"]["adaylar"][0]["tarla_bahce_baskilandi"] = True
    blocked_row = bridge(blocked, onset)["bolgeler"]["cesme"]["adaylar"][0]
    assert blocked_row["onset_sar_pozitif_destek"] is False
    assert blocked_row["ana_esik_pozitif_destekli_diagnostik"] is False

    unsafe = json.loads(json.dumps(onset))
    unsafe["kritik_regresyon"][0]["onset_sar_guclu_lokal_destek"] = True
    unsafe_payload = bridge(positive, unsafe)
    unsafe_row = unsafe_payload["bolgeler"]["cesme"]["adaylar"][0]
    assert unsafe_payload["onset_sar_pozitif_kopru_aktif"] is False
    assert unsafe_row["onset_sar_pozitif_destek"] is False
    assert unsafe_row["ana_esik_pozitif_destekli_diagnostik"] is False


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    _self_check()
    if args.check_only:
        print("foundation excavation onset SAR positive bridge self-check: ok")
        return 0

    payload = bridge(_load(POSITIVE_JSON), _load(ONSET_SAR_JSON))
    OUTPUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
