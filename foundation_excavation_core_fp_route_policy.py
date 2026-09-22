"""Rota FP-klon kapısında çekirdek 1+4 regresyon setini önceliklendirir.

Kullanıcının açıkça verdiği dört tarla/bahçe yanlış-pozitifi sert spektral klon
veto setidir. Daha eski/tarihsel yardımcı yanlış-pozitifler diagnostik olarak
saklanır, fakat tek başına rota morfolojisini veto etmez.

Sınırdaki bir aday doğrulanmış kazı referansına çok sıkı biçimde benziyorsa,
yalnız çekirdek FP uzaklığının 0.25 sınırına değmesi otomatik veto sayılmaz.
Bu istisna yalnız pozitif uzaklık <=0.10 ve aynı zamanda çekirdek FP uzaklığının
%40'ından küçük/eşitse uygulanır. İstisna yeni kanıt üretmez; sadece FP-klon
etiketinin gerçek pozitif referansa çok daha yakın bir adayı erkenden boğmasını
önler. Çoklu-kanıt/SAR-temporal, tarla-bahçe, mevcut-müşteri ve alan kapıları
aynen korunur.

Bu katman yeni alarm veya saha görevi üretmez; mevcut 250 m² ana ve 150–249 m²
MİKRO eşiklerini değiştirmez.
"""

from __future__ import annotations

import argparse
import json

import foundation_excavation_route_gate_audit as base


CORE_FP_DISTANCE_KEY = "cekirdek_regresyon_yanlis_pozitif_uzakligi"
CORE_FP_ID_KEY = "cekirdek_regresyon_yanlis_pozitif_id"
CORE_POSITIVE_DISTANCE_KEY = "cekirdek_regresyon_dogrulanmis_kazi_uzakligi"
CORE_POSITIVE_ID_KEY = "cekirdek_regresyon_dogrulanmis_kazi_id"
AUX_FP_DISTANCE_KEY = "yardimci_yanlis_pozitif_uzakligi"
AUX_FP_ID_KEY = "yardimci_yanlis_pozitif_id"
DEFAULT_FEEDBACK_RADIUS_M = 30.0
POSITIVE_OVERRIDE_MAX_DISTANCE = 0.10
POSITIVE_OVERRIDE_MAX_RATIO = 0.40
_BASE_FEEDBACK_EFFECT = base._feedback_effect


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _feedback_effect_exact_radius(candidate, scene_date, feedback):
    """Saha geri bildirimini kayıttaki gerçek yarıçapla sınırlar.

    Temel rota audit'indeki 45 m SAR eşleme toleransı geri bildirim yarıçapına
    taşınmamalıdır. Böylece 25–45 m uzaktaki komşu parsel yalnız bilinen bir
    yanlış-pozitife veya mevcut müşteriye yakın diye bastırılmaz.
    """
    candidate_point = base._point(candidate)
    if candidate_point is None:
        return {
            "engel": False,
            "mevcut_musteri": False,
            "geri_bildirim": None,
        }

    in_radius = []
    for item in feedback:
        item_point = base._point(item)
        if item_point is None:
            continue
        radius = _num(item.get("eslesme_yaricapi_m"))
        if radius is None or radius <= 0:
            radius = DEFAULT_FEEDBACK_RADIUS_M
        if base._distance_m(candidate_point, item_point) <= radius:
            in_radius.append(item)

    if not in_radius:
        return {
            "engel": False,
            "mevcut_musteri": False,
            "geri_bildirim": None,
        }

    # Tarih ve sonuç semantiğini değiştirmeden yalnız aday kayıt havuzunu
    # gerçek saha yarıçapına daraltıyoruz.
    return _BASE_FEEDBACK_EFFECT(candidate, scene_date, in_radius)


def _positive_reference_override(similarity):
    """Çok sıkı pozitif benzerlikte yalnız FP-klon vetosunu kaldırır.

    Bu fonksiyon adayın rota/pozitif-kanıt statüsünü yükseltmez. Yalnız aday,
    doğrulanmış kazıya mutlak olarak çok yakın ve çekirdek FP'ye göre açıkça daha
    yakınsa 0.25 FP sınırının tek başına morfolojiyi false yapmasını engeller.
    """
    positive_distance = _num(similarity.get(CORE_POSITIVE_DISTANCE_KEY))
    core_distance = _num(similarity.get(CORE_FP_DISTANCE_KEY))
    if positive_distance is None or core_distance is None or core_distance <= 0:
        return False
    return bool(
        positive_distance <= POSITIVE_OVERRIDE_MAX_DISTANCE
        and positive_distance <= core_distance * POSITIVE_OVERRIDE_MAX_RATIO
    )


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
    positive_override = _positive_reference_override(similarity)
    core_suppressed = bool(
        core_distance is not None
        and core_distance <= base.FP_SPECTRAL_CLONE_MAX_DISTANCE
        and not positive_override
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
    total_positive_overrides = 0

    for region_key, route_region in (payload.get("bolgeler") or {}).items():
        similarity_rows = [
            item
            for item in (regions.get(region_key) or {}).get("adaylar") or []
            if isinstance(item, dict) and base._point(item) is not None
        ]
        core_count = 0
        aux_only_count = 0
        positive_override_count = 0
        for row in route_region.get("adaylar") or []:
            similarity, match_distance = base._nearest(row, similarity_rows)
            if (
                similarity is None
                or match_distance is None
                or match_distance > base.REFERENCE_SIMILARITY_MATCH_RADIUS_M
            ):
                row["cekirdek_fp_id"] = None
                row["cekirdek_fp_uzakligi"] = None
                row["cekirdek_pozitif_id"] = None
                row["cekirdek_pozitif_uzakligi"] = None
                row["cekirdek_pozitif_override"] = False
                row["yardimci_fp_id"] = None
                row["yardimci_fp_uzakligi"] = None
                row["yalniz_yardimci_fp_benzerligi"] = False
                continue

            core_distance = _num(similarity.get(CORE_FP_DISTANCE_KEY))
            positive_distance = _num(similarity.get(CORE_POSITIVE_DISTANCE_KEY))
            aux_distance = _num(similarity.get(AUX_FP_DISTANCE_KEY))
            positive_override = _positive_reference_override(similarity)
            core_blocks = bool(
                core_distance is not None
                and core_distance <= base.FP_SPECTRAL_CLONE_MAX_DISTANCE
                and not positive_override
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
            row["cekirdek_pozitif_id"] = similarity.get(CORE_POSITIVE_ID_KEY)
            row["cekirdek_pozitif_uzakligi"] = (
                round(positive_distance, 3) if positive_distance is not None else None
            )
            row["cekirdek_pozitif_override"] = positive_override
            row["yardimci_fp_id"] = similarity.get(AUX_FP_ID_KEY)
            row["yardimci_fp_uzakligi"] = (
                round(aux_distance, 3) if aux_distance is not None else None
            )
            row["yalniz_yardimci_fp_benzerligi"] = aux_only
            if row.get("yanlis_pozitif_spektral_klon_baskilandi"):
                core_count += 1
            if aux_only:
                aux_only_count += 1
            if positive_override:
                positive_override_count += 1

        route_region["cekirdek_fp_klon_baskilanan_sayisi"] = core_count
        route_region["yalniz_yardimci_fp_benzerligi_sayisi"] = aux_only_count
        route_region["cekirdek_pozitif_override_sayisi"] = positive_override_count
        total_core_suppressed += core_count
        total_aux_only += aux_only_count
        total_positive_overrides += positive_override_count

    payload["surum"] = max(int(payload.get("surum") or 0), 7)
    payload["fp_klon_politikasi"] = (
        "Kullanıcının dört çekirdek yanlış-pozitifi sert veto; fakat doğrulanmış kazı "
        "referansına <=0.10 uzaklıkta ve çekirdek FP uzaklığının <=%40'ı kadar yakın "
        "olan aday yalnız FP-klon vetosundan muaf; tarihsel yardımcı yanlış-pozitif "
        "tek başına yalnız diagnostik negatif kanıt"
    )
    payload["geri_bildirim_yaricap_politikasi"] = (
        "Saha kaydındaki eslesme_yaricapi_m aynen kullanılır; SAR 45 m toleransı "
        "saha yanlış-pozitifi/mevcut-müşteri engeline taşınmaz"
    )
    payload["pozitif_override_maks_uzaklik"] = POSITIVE_OVERRIDE_MAX_DISTANCE
    payload["pozitif_override_maks_fp_orani"] = POSITIVE_OVERRIDE_MAX_RATIO
    payload["toplam_cekirdek_fp_klon_baskilanan"] = total_core_suppressed
    payload["toplam_yalniz_yardimci_fp_benzerligi"] = total_aux_only
    payload["toplam_cekirdek_pozitif_override"] = total_positive_overrides
    payload["not"] = (
        "Rota kapısı yalnız diagnostiktir. 250 m² ana eşik ve 150–249 m² MİKRO "
        "politikası değişmez. Tarla/bahçe morfoloji negatifleri ve kullanıcının dört "
        "çekirdek yanlış-pozitifine 0.25 veya daha yakın spektral klonlar sert veto "
        "olarak kalır; yalnız doğrulanmış kazı referansına <=0.10 mutlak uzaklıkta ve "
        "çekirdek FP'ye göre en az 2.5 kat daha yakın aday FP-klon etiketinden muaf olur. "
        "Bu muafiyet başlı başına pozitif kanıt değildir ve saha görevi üretmez; adayın "
        "çoklu-kanıt, SAR/temporal, saha geri bildirimi ve kalibrasyon kapıları aynen "
        "uygulanır. Tarihsel yardımcı yanlış-pozitif benzerliği tek başına veto değildir; "
        "saha geri bildiriminde yalnız kayıttaki gerçek 25/30 m yarıçap kullanılır."
    )
    return payload


def audit(morphology=None, sar=None, feedback=None, reference_similarity=None):
    if reference_similarity is None:
        reference_similarity = base._load(base.REFERENCE_SIMILARITY_JSON)

    original_fp = base._fp_spectral_clone_effect
    original_feedback = base._feedback_effect
    base._fp_spectral_clone_effect = _core_fp_spectral_clone_effect
    base._feedback_effect = _feedback_effect_exact_radius
    try:
        payload = base.audit(morphology, sar, feedback, reference_similarity)
    finally:
        base._fp_spectral_clone_effect = original_fp
        base._feedback_effect = original_feedback
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
        CORE_POSITIVE_ID_KEY: "TP-1",
        CORE_POSITIVE_DISTANCE_KEY: 0.20,
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
        CORE_POSITIVE_ID_KEY: "TP-1",
        CORE_POSITIVE_DISTANCE_KEY: 0.18,
        AUX_FP_ID_KEY: "AUX",
        AUX_FP_DISTANCE_KEY: 0.20,
    }]
    effect = _core_fp_spectral_clone_effect(candidate, core_clone)
    assert effect["baskilandi"] is True

    # Regresyon: doğrulanmış kazıya neredeyse birebir benzeyen aday, yalnız 0.25
    # FP sınırına değdiği için erkenden boğulmamalı. Bu yalnız veto muafiyetidir.
    tight_positive = [{
        "enlem": 38.3,
        "boylam": 26.3,
        "en_yakin_yanlis_pozitif_id": "FP-CORE-1",
        "yanlis_pozitif_uzakligi": 0.25,
        CORE_FP_ID_KEY: "FP-CORE-1",
        CORE_FP_DISTANCE_KEY: 0.25,
        CORE_POSITIVE_ID_KEY: "TP-1",
        CORE_POSITIVE_DISTANCE_KEY: 0.027,
        AUX_FP_ID_KEY: "AUX",
        AUX_FP_DISTANCE_KEY: 0.24,
    }]
    effect = _core_fp_spectral_clone_effect(candidate, tight_positive)
    assert effect["baskilandi"] is False
    assert _positive_reference_override(tight_positive[0]) is True

    # Bilinen FP'nin kendisi gibi FP uzaklığı sıfır olan kayıt asla override edilmez.
    fp_identity = dict(tight_positive[0])
    fp_identity[CORE_FP_DISTANCE_KEY] = 0.0
    assert _positive_reference_override(fp_identity) is False

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
    assert row["cekirdek_pozitif_override"] is False
    assert payload["toplam_yalniz_yardimci_fp_benzerligi"] == 1
    assert payload["toplam_cekirdek_fp_klon_baskilanan"] == 0

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
    reference_similarity = {"bolgeler": {"cesme": {"adaylar": tight_positive}}}
    payload = _annotate_policy(payload, reference_similarity)
    row = payload["bolgeler"]["cesme"]["adaylar"][0]
    assert row["cekirdek_pozitif_override"] is True
    assert payload["toplam_cekirdek_pozitif_override"] == 1

    # Regresyon: 25 m saha yarıçapı SAR'ın 45 m eşleme toleransına genişlememeli.
    fp = [{
        "id": "FP-RADIUS",
        "sonuc": "YANLIS_POZITIF",
        "sonuc_tarihi": "2026-09-15",
        "enlem": 38.3,
        "boylam": 26.3,
        "eslesme_yaricapi_m": 25,
    }]
    scene_date = base._date("2026-09-15")
    inside = {"enlem": 38.30018, "boylam": 26.3}
    outside = {"enlem": 38.30032, "boylam": 26.3}
    assert _feedback_effect_exact_radius(inside, scene_date, fp)["engel"] is True
    assert _feedback_effect_exact_radius(outside, scene_date, fp)["engel"] is False


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
