"""Rota FP-klon kapısında çekirdek 1+4 regresyon setini önceliklendirir.

Kullanıcının açıkça verdiği dört tarla/bahçe yanlış-pozitifi sert spektral klon
veto setidir. Daha eski/tarihsel yardımcı yanlış-pozitifler diagnostik olarak
saklanır, fakat tek başına rota morfolojisini veto etmez. Bu katman yeni alarm
veya saha görevi üretmez; mevcut 250 m² ana ve 150–249 m² MİKRO eşiklerini
ve çoklu-kanıt/SAR kapılarını değiştirmez.
"""

from __future__ import annotations

import argparse
import json

import foundation_excavation_route_gate_audit as base


CORE_FP_DISTANCE_KEY = "cekirdek_regresyon_yanlis_pozitif_uzakligi"
CORE_FP_ID_KEY = "cekirdek_regresyon_yanlis_pozitif_id"
AUX_FP_DISTANCE_KEY = "yardimci_yanlis_pozitif_uzakligi"
AUX_FP_ID_KEY = "yardimci_yanlis_pozitif_id"


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _core_fp_spectral_clone_effect(candidate, similarity_rows):
    similarity, match_distance = base._nearest(candidate, similarity_rows)
    if (
        similarity is None
        or match_distance is None
        or match_distance > base.REFERENCE_SIMILARITY_MATCH_RADIUS_M
    ):
        return {
            "baskilandi": False,
            "eslesme_mesafe_m": None,
            "yanlis_pozitif_id": None,
            "yanlis_pozitif_uzakligi": None,
        }

    legacy_distance = _num(similarity.get("yanlis_pozitif_uzakligi"))
    core_distance = _num(similarity.get(CORE_FP_DISTANCE_KEY))
    core_suppressed = bool(
        core_distance is not None
        and core_distance <= base.FP_SPECTRAL_CLONE_MAX_DISTANCE
    )
    return {
        "baskilandi": core_suppressed,
        "eslesme_mesafe_m": round(match_distance, 1),
        # Geriye dönük diagnostik alanlar en yakın tüm-FP referansını korur.
        "yanlis_pozitif_id": similarity.get("en_yakin_yanlis_pozitif_id"),
        "yanlis_pozitif_uzakligi": (
            round(legacy_distance, 3) if legacy_distance is not None else None
        ),
    }


def _annotate_policy(payload, reference_similarity):
    regions = (reference_similarity or {}).get("bolgeler") or {}
    total_core_suppressed = 0
    total_aux_only = 0

    for region_key, route_region in (payload.get("bolgeler") or {}).items():
        similarity_rows = [
            item
            for item in (regions.get(region_key) or {}).get("adaylar") or []
            if isinstance(item, dict) and base._point(item) is not None
        ]
        core_count = 0
        aux_only_count = 0
        for row in route_region.get("adaylar") or []:
            similarity, match_distance = base._nearest(row, similarity_rows)
            if (
                similarity is None
                or match_distance is None
                or match_distance > base.REFERENCE_SIMILARITY_MATCH_RADIUS_M
            ):
                row["cekirdek_fp_id"] = None
                row["cekirdek_fp_uzakligi"] = None
                row["yardimci_fp_id"] = None
                row["yardimci_fp_uzakligi"] = None
                row["yalniz_yardimci_fp_benzerligi"] = False
                continue

            core_distance = _num(similarity.get(CORE_FP_DISTANCE_KEY))
            aux_distance = _num(similarity.get(AUX_FP_DISTANCE_KEY))
            core_blocks = bool(
                core_distance is not None
                and core_distance <= base.FP_SPECTRAL_CLONE_MAX_DISTANCE
            )
            aux_blocks = bool(
                aux_distance is not None
                and aux_distance <= base.FP_SPECTRAL_CLONE_MAX_DISTANCE
            )
            aux_only = bool(aux_blocks and not core_blocks)

            row["cekirdek_fp_id"] = similarity.get(CORE_FP_ID_KEY)
            row["cekirdek_fp_uzakligi"] = (
                round(core_distance, 3) if core_distance is not None else None
            )
            row["yardimci_fp_id"] = similarity.get(AUX_FP_ID_KEY)
            row["yardimci_fp_uzakligi"] = (
                round(aux_distance, 3) if aux_distance is not None else None
            )
            row["yalniz_yardimci_fp_benzerligi"] = aux_only
            if row.get("yanlis_pozitif_spektral_klon_baskilandi"):
                core_count += 1
            if aux_only:
                aux_only_count += 1

        route_region["cekirdek_fp_klon_baskilanan_sayisi"] = core_count
        route_region["yalniz_yardimci_fp_benzerligi_sayisi"] = aux_only_count
        total_core_suppressed += core_count
        total_aux_only += aux_only_count

    payload["surum"] = max(int(payload.get("surum") or 0), 5)
    payload["fp_klon_politikasi"] = (
        "Kullanıcının dört çekirdek yanlış-pozitifi sert veto; tarihsel yardımcı "
        "yanlış-pozitif tek başına yalnız diagnostik negatif kanıt"
    )
    payload["toplam_cekirdek_fp_klon_baskilanan"] = total_core_suppressed
    payload["toplam_yalniz_yardimci_fp_benzerligi"] = total_aux_only
    payload["not"] = (
        "Rota kapısı yalnız diagnostiktir. 250 m² ana eşik ve 150–249 m² MİKRO "
        "politikası değişmez. Tarla/bahçe morfoloji negatifleri ve kullanıcının dört "
        "çekirdek yanlış-pozitifine 0.25 veya daha yakın spektral klonlar sert veto "
        "olarak kalır. Tarihsel yardımcı yanlış-pozitif benzerliği tek başına veto "
        "değildir; adayın çoklu-kanıt, SAR, saha geri bildirimi ve kalibrasyon kapıları "
        "aynen uygulanır. En az iki zamansal geçerli doğrulanmış kazı referansı olmadan "
        "rota kapısı açılmaz."
    )
    return payload


def audit(morphology=None, sar=None, feedback=None, reference_similarity=None):
    if reference_similarity is None:
        reference_similarity = base._load(base.REFERENCE_SIMILARITY_JSON)

    original = base._fp_spectral_clone_effect
    base._fp_spectral_clone_effect = _core_fp_spectral_clone_effect
    try:
        payload = base.audit(morphology, sar, feedback, reference_similarity)
    finally:
        base._fp_spectral_clone_effect = original
    return _annotate_policy(payload, reference_similarity)


def _self_check():
    candidate = {"enlem": 38.3, "boylam": 26.3}
    aux_only = [{
        "enlem": 38.3,
        "boylam": 26.3,
        "en_yakin_yanlis_pozitif_id": "AUX",
        "yanlis_pozitif_uzakligi": 0.20,
        CORE_FP_ID_KEY: "FP-CORE-1",
        CORE_FP_DISTANCE_KEY: 0.31,
        AUX_FP_ID_KEY: "AUX",
        AUX_FP_DISTANCE_KEY: 0.20,
    }]
    effect = _core_fp_spectral_clone_effect(candidate, aux_only)
    assert effect["baskilandi"] is False

    core_clone = [{
        "enlem": 38.3,
        "boylam": 26.3,
        "en_yakin_yanlis_pozitif_id": "AUX",
        "yanlis_pozitif_uzakligi": 0.20,
        CORE_FP_ID_KEY: "FP-CORE-1",
        CORE_FP_DISTANCE_KEY: 0.24,
        AUX_FP_ID_KEY: "AUX",
        AUX_FP_DISTANCE_KEY: 0.20,
    }]
    effect = _core_fp_spectral_clone_effect(candidate, core_clone)
    assert effect["baskilandi"] is True

    payload = {
        "surum": 4,
        "bolgeler": {
            "cesme": {
                "adaylar": [{
                    "enlem": 38.3,
                    "boylam": 26.3,
                    "yanlis_pozitif_spektral_klon_baskilandi": False,
                }]
            }
        },
    }
    reference_similarity = {"bolgeler": {"cesme": {"adaylar": aux_only}}}
    payload = _annotate_policy(payload, reference_similarity)
    row = payload["bolgeler"]["cesme"]["adaylar"][0]
    assert row["yalniz_yardimci_fp_benzerligi"] is True
    assert payload["toplam_yalniz_yardimci_fp_benzerligi"] == 1
    assert payload["toplam_cekirdek_fp_klon_baskilanan"] == 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    _self_check()
    if args.check_only:
        print("foundation excavation core FP route policy self-check: ok")
        return

    payload = audit()
    base.OUTPUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
