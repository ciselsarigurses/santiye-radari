"""Çekirdek 1+4 regresyon setinin rota FP bastırmasıyla tutarlılığını denetler.

Bu katman yalnız diagnostiktir; alarm, saha görevi veya üretim filtresi üretmez.
Amaç, kullanıcının açıkça verdiği dört yanlış-pozitif referans yerine tarihsel/yardımcı
bir yanlış-pozitifin 0.25 spektral-klon eşiğiyle adayı tek başına bastırıp bastırmadığını
ölçmektir. Böyle bir durum recall kaybı riski olarak raporlanır ama otomatik olarak
rota kararını değiştirmez.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


BASE = Path(__file__).resolve().parent
ROUTE_JSON = BASE / "foundation_excavation_route_gate_review.json"
SIMILARITY_JSON = BASE / "foundation_excavation_reference_similarity_review.json"
OUTPUT_JSON = BASE / "foundation_excavation_core_fp_consistency_review.json"
FP_CLONE_THRESHOLD = 0.25


def _load(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coord(item):
    try:
        return round(float(item["enlem"]), 6), round(float(item["boylam"]), 6)
    except (KeyError, TypeError, ValueError):
        return None


def _similarity_index(region):
    result = {}
    for item in (region or {}).get("adaylar") or []:
        if isinstance(item, dict) and _coord(item) is not None:
            result[_coord(item)] = item
    return result


def _analyze_region(route_region, similarity_region):
    similarity_by_coord = _similarity_index(similarity_region)
    rows = []
    unmatched = 0

    for route in (route_region or {}).get("adaylar") or []:
        if not isinstance(route, dict) or not route.get("yanlis_pozitif_spektral_klon_baskilandi"):
            continue
        point = _coord(route)
        similarity = similarity_by_coord.get(point)
        if similarity is None:
            unmatched += 1
            continue

        legacy_distance = _num(similarity.get("yanlis_pozitif_uzakligi"))
        core_distance = _num(similarity.get("cekirdek_regresyon_yanlis_pozitif_uzakligi"))
        legacy_id = similarity.get("en_yakin_yanlis_pozitif_id")
        core_id = similarity.get("cekirdek_regresyon_yanlis_pozitif_id")
        auxiliary_id = similarity.get("yardimci_yanlis_pozitif_id")
        auxiliary_distance = _num(similarity.get("yardimci_yanlis_pozitif_uzakligi"))

        legacy_blocks = legacy_distance is not None and legacy_distance <= FP_CLONE_THRESHOLD
        core_blocks = core_distance is not None and core_distance <= FP_CLONE_THRESHOLD
        auxiliary_only = bool(
            legacy_blocks
            and not core_blocks
            and auxiliary_id is not None
            and legacy_id == auxiliary_id
        )
        rows.append(
            {
                "enlem": route.get("enlem"),
                "boylam": route.get("boylam"),
                "spektral_etki_alani_m2": route.get("spektral_etki_alani_m2"),
                "morfoloji_puani": route.get("morfoloji_puani"),
                "mevcut_bastirma_fp_id": legacy_id,
                "mevcut_bastirma_uzakligi": legacy_distance,
                "cekirdek_fp_id": core_id,
                "cekirdek_fp_uzakligi": core_distance,
                "yardimci_fp_id": auxiliary_id,
                "yardimci_fp_uzakligi": auxiliary_distance,
                "yalniz_yardimci_fp_nedeniyle_bastiriliyor": auxiliary_only,
            }
        )

    auxiliary_only_rows = [
        item for item in rows if item["yalniz_yardimci_fp_nedeniyle_bastiriliyor"]
    ]
    return {
        "mevcut_fp_klon_bastirma_sayisi": len(rows),
        "yalniz_yardimci_fp_nedeniyle_bastirilan_sayi": len(auxiliary_only_rows),
        "cekirdek_fp_de_bastiran_sayi": sum(
            1
            for item in rows
            if item["cekirdek_fp_uzakligi"] is not None
            and item["cekirdek_fp_uzakligi"] <= FP_CLONE_THRESHOLD
        ),
        "eslesmeyen_bastirilmis_aday": unmatched,
        "yalniz_yardimci_fp_adaylari": auxiliary_only_rows,
    }


def audit(route=None, similarity=None):
    route = route if isinstance(route, dict) else _load(ROUTE_JSON)
    similarity = similarity if isinstance(similarity, dict) else _load(SIMILARITY_JSON)
    route_regions = route.get("bolgeler") or {}
    similarity_regions = similarity.get("bolgeler") or {}

    regions = {}
    for region_key in sorted(set(route_regions) | set(similarity_regions)):
        regions[region_key] = _analyze_region(
            route_regions.get(region_key) or {},
            similarity_regions.get(region_key) or {},
        )

    auxiliary_only_total = sum(
        region["yalniz_yardimci_fp_nedeniyle_bastirilan_sayi"]
        for region in regions.values()
    )
    core_ids = similarity.get("cekirdek_regresyon_yanlis_pozitif_idleri") or []
    return {
        "surum": 1,
        "amac": "Rota FP-klon bastırmasının kullanıcının çekirdek 1+4 regresyon setiyle tutarlılığını ölçmek",
        "fp_klon_esigi": FP_CLONE_THRESHOLD,
        "cekirdek_regresyon_yanlis_pozitif_idleri": core_ids,
        "cekirdek_regresyon_tam": similarity.get("cekirdek_regresyon_tam"),
        "yalniz_yardimci_fp_nedeniyle_bastirilan_toplam": auxiliary_only_total,
        "recall_riski_var": auxiliary_only_total > 0,
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "bolgeler": regions,
        "not": (
            "Bu çıktı rota kararını değiştirmez. Yalnız yardımcı/tarihsel bir yanlış-pozitifin, "
            "kullanıcının dört çekirdek negatifinden hiçbiri 0.25 eşiğinde değilken adayı bastırdığı "
            "durumları recall riski olarak işaretler."
        ),
    }


def _self_check():
    route = {
        "bolgeler": {
            "cesme": {
                "adaylar": [
                    {
                        "enlem": 38.1,
                        "boylam": 26.1,
                        "yanlis_pozitif_spektral_klon_baskilandi": True,
                        "spektral_etki_alani_m2": 300,
                        "morfoloji_puani": 90,
                    },
                    {
                        "enlem": 38.2,
                        "boylam": 26.2,
                        "yanlis_pozitif_spektral_klon_baskilandi": True,
                    },
                ]
            }
        }
    }
    similarity = {
        "cekirdek_regresyon_tam": True,
        "cekirdek_regresyon_yanlis_pozitif_idleri": ["FP1", "FP2", "FP3", "FP4"],
        "bolgeler": {
            "cesme": {
                "adaylar": [
                    {
                        "enlem": 38.1,
                        "boylam": 26.1,
                        "en_yakin_yanlis_pozitif_id": "AUX",
                        "yanlis_pozitif_uzakligi": 0.24,
                        "cekirdek_regresyon_yanlis_pozitif_id": "FP1",
                        "cekirdek_regresyon_yanlis_pozitif_uzakligi": 0.26,
                        "yardimci_yanlis_pozitif_id": "AUX",
                        "yardimci_yanlis_pozitif_uzakligi": 0.24,
                    },
                    {
                        "enlem": 38.2,
                        "boylam": 26.2,
                        "en_yakin_yanlis_pozitif_id": "FP2",
                        "yanlis_pozitif_uzakligi": 0.20,
                        "cekirdek_regresyon_yanlis_pozitif_id": "FP2",
                        "cekirdek_regresyon_yanlis_pozitif_uzakligi": 0.20,
                        "yardimci_yanlis_pozitif_id": "AUX",
                        "yardimci_yanlis_pozitif_uzakligi": 0.30,
                    },
                ]
            }
        },
    }
    payload = audit(route, similarity)
    assert payload["yalniz_yardimci_fp_nedeniyle_bastirilan_toplam"] == 1
    assert payload["recall_riski_var"] is True
    assert payload["bolgeler"]["cesme"]["cekirdek_fp_de_bastiran_sayi"] == 1
    assert len(payload["bolgeler"]["cesme"]["yalniz_yardimci_fp_adaylari"]) == 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("foundation_excavation_core_fp_consistency_audit: OK")
        return
    payload = audit()
    OUTPUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
