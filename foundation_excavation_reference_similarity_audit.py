"""Temel/kepçe kazısı adaylarını saha referanslarına spektral benzerlikle sıralar.

Bu katman YALNIZCA diagnostiktir; alarm, saha görevi veya üretim filtresi üretmez.
Amaç seed-merkezli Sentinel-2 adaylarının, doğrulanmış gerçek kazının ham 5x5
imzasına mı yoksa sahada yanlış-pozitif çıkan tarla/bahçe örneklerine mi daha yakın
olduğunu ölçmektir.

Sentinel-2 10 m veriden gerçek kazı derinliği ölçülmez. Bir DOGRULANMIS_KAZI
referansı ancak kullanılan Sentinel-2 son sahnesi saha doğrulama tarihine eşit veya
daha yeniyse pozitif spektral referans sayılır. Böylece kazının sahada doğrulandığı
tarihten daha eski görüntü, yanlışlıkla "kazı imzası" olarak öğrenilmez.

Kullanıcının açıkça verdiği regresyon seti 1 gerçek kazı + 4 tarla/bahçe yanlış
pozitifinden oluşur. Diğer tarihsel yanlış pozitifler genel bastırma diagnostiginde
korunur ancak çekirdek regresyon marjını veto etmez. Böylece bağımsız bir bahçe
temizliği örneği, kullanıcının tanımladığı dört negatif örneğin yerine geçmez.

Tek doğrulanmış kazı referansı bulunduğu için bu sonuç bir sınıflandırıcı değildir
ve rota kapısını tek başına açamaz. 250 m² ana eşik ile 150–249 m² MİKRO politikası
değişmez.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path


BASE = Path(__file__).resolve().parent
MORPHOLOGY_JSON = BASE / "seed_centered_excavation_morphology_review.json"
REFERENCE_JSON = BASE / "excavation_reference_signature_review.json"
FIELD_FEEDBACK_JSON = BASE / "manual_field_feedback.json"
OUTPUT_JSON = BASE / "foundation_excavation_reference_similarity_review.json"

MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]

# Kullanıcının açıkça "bu beş örneği regresyon seti olarak kullan" dediği çekirdek set.
CORE_POSITIVE_IDS = frozenset({"FN-20260916-CESME-001"})
CORE_NEGATIVE_IDS = frozenset(
    {
        "FP-20260916-REISDERE-001",
        "FP-20260917-UZUNKUYU-001",
        "FP-20260917-MUSALLA-001",
        "FP-20260917-MUSALLA-002",
    }
)

# Farkların karşılaştırılabilir hale gelmesi için kaba, sabit ölçekler. Bunlar
# model parametresi değil; yalnız diagnostik uzaklığı normalize eder.
FEATURES = (
    ("ortalama_rgb_5x5", "ortalama_rgb_farki", 0.10),
    ("max_rgb_5x5", "max_rgb_farki", 0.20),
    ("parlak_artis_orani_5x5", "parlak_artis_orani", 0.30),
    ("koyulasma_orani_5x5", "koyulasma_orani", 0.25),
    ("soil_mask_orani_5x5", "soil_mask_orani", 0.20),
)

POSITIVE_MARGIN_MIN = 0.35
POSITIVE_DISTANCE_MAX = 1.25


def _load(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    return None


def _feedback_dates(payload):
    result = {}
    for item in (payload.get("kayitlar") or []):
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            continue
        result[item_id] = {
            "sonuc": str(item.get("sonuc") or "").upper(),
            "sonuc_tarihi": _parse_date(item.get("sonuc_tarihi")),
        }
    return result


def _candidate_vector(candidate):
    values = []
    for candidate_key, _, _ in FEATURES:
        try:
            values.append(float(candidate[candidate_key]))
        except (KeyError, TypeError, ValueError):
            return None
    return values


def _reference_vector(reference):
    context = reference.get("cevre_5x5") or {}
    values = []
    for _, reference_key, _ in FEATURES:
        try:
            values.append(float(context[reference_key]))
        except (KeyError, TypeError, ValueError):
            return None
    return values


def _distance(candidate_vector, reference_vector):
    terms = []
    for candidate_value, reference_value, (_, _, scale) in zip(
        candidate_vector, reference_vector, FEATURES
    ):
        normalized = abs(candidate_value - reference_value) / scale
        # Tek aykırı özellik bütün diagnostik uzaklığı sonsuza sürüklemesin.
        terms.append(min(normalized, 3.0) ** 2)
    return math.sqrt(sum(terms) / max(len(terms), 1))


def _flatten_references(payload, field_feedback=None):
    positives = []
    negatives = []
    excluded_positive = []
    feedback_by_id = _feedback_dates(field_feedback or {})

    for region_key, region in (payload.get("bolgeler") or {}).items():
        if not isinstance(region, dict):
            continue
        scene_date = _parse_date(region.get("son_tarih"))
        for item in region.get("referanslar") or []:
            if not isinstance(item, dict):
                continue
            vector = _reference_vector(item)
            if vector is None:
                continue
            item_id = str(item.get("id") or "").strip()
            row = {
                "id": item_id or None,
                "sonuc": str(item.get("sonuc") or "").upper(),
                "bolge": region_key,
                "enlem": item.get("enlem"),
                "boylam": item.get("boylam"),
                "vector": vector,
            }
            if row["sonuc"] == "DOGRULANMIS_KAZI":
                feedback = feedback_by_id.get(item_id) or {}
                field_date = feedback.get("sonuc_tarihi")
                temporally_valid = bool(
                    scene_date is not None
                    and field_date is not None
                    and scene_date >= field_date
                )
                if temporally_valid:
                    positives.append(row)
                else:
                    excluded_positive.append(
                        {
                            "id": row["id"],
                            "bolge": region_key,
                            "sentinel_son_tarih": (
                                scene_date.isoformat() if scene_date else None
                            ),
                            "saha_dogrulama_tarihi": (
                                field_date.isoformat() if field_date else None
                            ),
                            "neden": (
                                "SENTINEL_SAHNESI_SAHA_DOGRULAMASINDAN_"
                                "ESKI_VEYA_TARIH_EKSIK"
                            ),
                        }
                    )
            elif row["sonuc"] == "YANLIS_POZITIF":
                # Yanlış-pozitif referansı, saha geri bildiriminin özellikle o eski
                # radar sinyalini reddetmesi nedeniyle tarihsel olarak kullanılabilir.
                negatives.append(row)
    return positives, negatives, excluded_positive


def _partition_core_references(positives, negatives):
    """Kullanıcının beşli regresyon setini diğer tarihsel referanslardan ayır."""
    core_positives = [
        item for item in positives if str(item.get("id") or "") in CORE_POSITIVE_IDS
    ]
    core_negatives = [
        item for item in negatives if str(item.get("id") or "") in CORE_NEGATIVE_IDS
    ]
    auxiliary_negatives = [
        item for item in negatives if str(item.get("id") or "") not in CORE_NEGATIVE_IDS
    ]
    return core_positives, core_negatives, auxiliary_negatives


def _nearest(vector, references):
    nearest = None
    nearest_distance = None
    for reference in references:
        distance = _distance(vector, reference["vector"])
        if nearest_distance is None or distance < nearest_distance:
            nearest = reference
            nearest_distance = distance
    return nearest, nearest_distance


def _candidate_area(candidate):
    try:
        return int(candidate.get("spektral_etki_alani_m2") or 0)
    except (TypeError, ValueError):
        return 0


def _positive_like(positive_distance, negative_distance):
    margin = None
    if positive_distance is not None and negative_distance is not None:
        margin = negative_distance - positive_distance
    positive_like = bool(
        positive_distance is not None
        and negative_distance is not None
        and positive_distance <= POSITIVE_DISTANCE_MAX
        and margin is not None
        and margin >= POSITIVE_MARGIN_MIN
    )
    return positive_like, margin


def _score_candidate(
    candidate,
    positives,
    negatives,
    core_positives=None,
    core_negatives=None,
    auxiliary_negatives=None,
):
    vector = _candidate_vector(candidate)
    if vector is None:
        return None

    positive, positive_distance = _nearest(vector, positives)
    negative, negative_distance = _nearest(vector, negatives)
    positive_like, margin = _positive_like(positive_distance, negative_distance)

    # Çekirdek regresyon skoru, yalnız kullanıcının açıkça verdiği 1+4 referansla
    # hesaplanır. Bu metrik diagnostiktir ve mevcut genel skorun semantiğini bozmaz.
    core_positive, core_positive_distance = _nearest(vector, core_positives or [])
    core_negative, core_negative_distance = _nearest(vector, core_negatives or [])
    core_positive_like, core_margin = _positive_like(
        core_positive_distance, core_negative_distance
    )
    auxiliary_negative, auxiliary_negative_distance = _nearest(
        vector, auxiliary_negatives or []
    )

    area_m2 = _candidate_area(candidate)
    main_band = area_m2 >= MAIN_THRESHOLD_M2
    micro_band = MICRO_RANGE_M2[0] <= area_m2 <= MICRO_RANGE_M2[1]

    return {
        "enlem": candidate.get("enlem"),
        "boylam": candidate.get("boylam"),
        "spektral_etki_alani_m2": area_m2,
        "morfoloji_puani": candidate.get("seed_merkezli_morfoloji_puani"),
        "morfoloji_seviyesi": candidate.get("seed_merkezli_morfoloji_seviyesi"),
        "ana_esik": main_band,
        "mikro_bant": micro_band,
        # Geriye dönük uyumluluk: mevcut alanlar tüm tarihsel negatifleri kullanır.
        "en_yakin_dogrulanmis_kazi_id": positive.get("id") if positive else None,
        "dogrulanmis_kazi_uzakligi": (
            round(positive_distance, 3) if positive_distance is not None else None
        ),
        "en_yakin_yanlis_pozitif_id": negative.get("id") if negative else None,
        "yanlis_pozitif_uzakligi": (
            round(negative_distance, 3) if negative_distance is not None else None
        ),
        "referans_ayrim_marji": round(margin, 3) if margin is not None else None,
        "dogrulanmis_kazi_referansina_daha_yakin": positive_like,
        # Yeni paralel çekirdek regresyon metriği: yalnız kullanıcının beşli seti.
        "cekirdek_regresyon_dogrulanmis_kazi_id": (
            core_positive.get("id") if core_positive else None
        ),
        "cekirdek_regresyon_dogrulanmis_kazi_uzakligi": (
            round(core_positive_distance, 3)
            if core_positive_distance is not None
            else None
        ),
        "cekirdek_regresyon_yanlis_pozitif_id": (
            core_negative.get("id") if core_negative else None
        ),
        "cekirdek_regresyon_yanlis_pozitif_uzakligi": (
            round(core_negative_distance, 3)
            if core_negative_distance is not None
            else None
        ),
        "cekirdek_regresyon_ayrim_marji": (
            round(core_margin, 3) if core_margin is not None else None
        ),
        "cekirdek_regresyon_dogrulanmis_kaziya_daha_yakin": core_positive_like,
        "yardimci_yanlis_pozitif_id": (
            auxiliary_negative.get("id") if auxiliary_negative else None
        ),
        "yardimci_yanlis_pozitif_uzakligi": (
            round(auxiliary_negative_distance, 3)
            if auxiliary_negative_distance is not None
            else None
        ),
        "alarm": False,
        "saha_gorevi": False,
    }


def audit(morphology=None, references=None, field_feedback=None):
    morphology = (
        morphology if isinstance(morphology, dict) else _load(MORPHOLOGY_JSON)
    )
    references = (
        references if isinstance(references, dict) else _load(REFERENCE_JSON)
    )
    field_feedback = (
        field_feedback
        if isinstance(field_feedback, dict)
        else _load(FIELD_FEEDBACK_JSON)
    )
    positives, negatives, excluded_positive = _flatten_references(
        references, field_feedback
    )
    core_positives, core_negatives, auxiliary_negatives = _partition_core_references(
        positives, negatives
    )

    regions = {}
    for region_key, region in (morphology.get("bolgeler") or {}).items():
        if not isinstance(region, dict):
            continue
        rows = []
        for candidate in region.get("adaylar") or []:
            if not isinstance(candidate, dict):
                continue
            scored = _score_candidate(
                candidate,
                positives,
                negatives,
                core_positives,
                core_negatives,
                auxiliary_negatives,
            )
            if scored is not None:
                rows.append(scored)
        rows.sort(
            key=lambda item: (
                bool(item["dogrulanmis_kazi_referansina_daha_yakin"]),
                float(item.get("referans_ayrim_marji") or -999),
                -float(item.get("dogrulanmis_kazi_uzakligi") or 999),
            ),
            reverse=True,
        )
        regions[region_key] = {
            "bolge": region.get("bolge") or region_key,
            "onceki_tarih": region.get("onceki_tarih"),
            "son_tarih": region.get("son_tarih"),
            "aday_sayisi": len(rows),
            "pozitif_referansa_daha_yakin_sayi": sum(
                1
                for item in rows
                if item["dogrulanmis_kazi_referansina_daha_yakin"]
            ),
            "cekirdek_regresyonda_pozitife_daha_yakin_sayi": sum(
                1
                for item in rows
                if item["cekirdek_regresyon_dogrulanmis_kaziya_daha_yakin"]
            ),
            "adaylar": rows,
        }

    core_complete = bool(
        len(core_positives) == len(CORE_POSITIVE_IDS)
        and len(core_negatives) == len(CORE_NEGATIVE_IDS)
    )

    return {
        "surum": 3,
        "amac": (
            "Seed-merkezli temel/kepçe adaylarını doğrulanmış kazı ve tarla/bahçe "
            "saha referanslarına spektral benzerlikle karşılaştırmak"
        ),
        "gercek_derinlik_olcumu": False,
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "dogrulanmis_kazi_referans_sayisi": len(positives),
        "yanlis_pozitif_referans_sayisi": len(negatives),
        "zamansal_gecersiz_dogrulanmis_kazi_referans_sayisi": len(
            excluded_positive
        ),
        "zamansal_gecersiz_dogrulanmis_kazi_referanslari": excluded_positive,
        "cekirdek_regresyon_pozitif_idleri": sorted(CORE_POSITIVE_IDS),
        "cekirdek_regresyon_yanlis_pozitif_idleri": sorted(CORE_NEGATIVE_IDS),
        "cekirdek_regresyon_pozitif_referans_sayisi": len(core_positives),
        "cekirdek_regresyon_yanlis_pozitif_referans_sayisi": len(core_negatives),
        "yardimci_yanlis_pozitif_referans_sayisi": len(auxiliary_negatives),
        "cekirdek_regresyon_tam": core_complete,
        "pozitif_marj_esigi": POSITIVE_MARGIN_MIN,
        "pozitif_uzaklik_tavani": POSITIVE_DISTANCE_MAX,
        "bolgeler": regions,
        "not": (
            "DOGRULANMIS_KAZI pozitif imzası yalnız kullanılan Sentinel son sahnesi "
            "saha doğrulama tarihine eşit veya daha yeniyse geçerlidir. Daha eski "
            "sahne, kazı başlamış gibi öğrenilmez. Kullanıcının beşli regresyon seti "
            "ayrı çekirdek metrikte 1 gerçek kazı + 4 belirtilen yanlış pozitif ile "
            "hesaplanır; diğer tarihsel yanlış pozitifler yalnız yardımcı bastırma "
            "diagnostiğidir. Tek doğrulanmış kazı referansı nedeniyle bu çıktı yalnız "
            "sıralama diagnostigidir. Rota, alarm veya saha görevi üretmez; SAR "
            "zorunlu kapı olarak yorumlanmaz."
        ),
    }


def _self_check():
    morphology = {
        "bolgeler": {
            "cesme": {
                "adaylar": [
                    {
                        "enlem": 38.1,
                        "boylam": 26.1,
                        "spektral_etki_alani_m2": 300,
                        "seed_merkezli_morfoloji_puani": 90,
                        "seed_merkezli_morfoloji_seviyesi": "YUKSEK",
                        "ortalama_rgb_5x5": 0.20,
                        "max_rgb_5x5": 0.40,
                        "parlak_artis_orani_5x5": 0.20,
                        "koyulasma_orani_5x5": 0.20,
                        "soil_mask_orani_5x5": 0.05,
                    },
                    {
                        "enlem": 38.2,
                        "boylam": 26.2,
                        "spektral_etki_alani_m2": 200,
                        "seed_merkezli_morfoloji_puani": 80,
                        "seed_merkezli_morfoloji_seviyesi": "YUKSEK",
                        "ortalama_rgb_5x5": 0.05,
                        "max_rgb_5x5": 0.12,
                        "parlak_artis_orani_5x5": 0.80,
                        "koyulasma_orani_5x5": 0.01,
                        "soil_mask_orani_5x5": 0.60,
                    },
                ]
            }
        }
    }
    references = {
        "bolgeler": {
            "cesme": {
                "son_tarih": "2026-09-17",
                "referanslar": [
                    {
                        "id": "TP",
                        "sonuc": "DOGRULANMIS_KAZI",
                        "enlem": 38.0,
                        "boylam": 26.0,
                        "cevre_5x5": {
                            "ortalama_rgb_farki": 0.20,
                            "max_rgb_farki": 0.40,
                            "parlak_artis_orani": 0.20,
                            "koyulasma_orani": 0.20,
                            "soil_mask_orani": 0.05,
                        },
                    },
                    {
                        "id": "FP",
                        "sonuc": "YANLIS_POZITIF",
                        "enlem": 38.3,
                        "boylam": 26.3,
                        "cevre_5x5": {
                            "ortalama_rgb_farki": 0.05,
                            "max_rgb_farki": 0.12,
                            "parlak_artis_orani": 0.80,
                            "koyulasma_orani": 0.01,
                            "soil_mask_orani": 0.60,
                        },
                    },
                ],
            }
        }
    }
    feedback = {
        "kayitlar": [
            {
                "id": "TP",
                "sonuc": "DOGRULANMIS_KAZI",
                "sonuc_tarihi": "2026-09-16",
            },
            {
                "id": "FP",
                "sonuc": "YANLIS_POZITIF",
                "sonuc_tarihi": "2026-09-16",
            },
        ]
    }
    payload = audit(morphology, references, feedback)
    rows = payload["bolgeler"]["cesme"]["adaylar"]
    by_lat = {row["enlem"]: row for row in rows}
    assert by_lat[38.1]["dogrulanmis_kazi_uzakligi"] == 0.0
    assert by_lat[38.1]["dogrulanmis_kazi_referansina_daha_yakin"] is True
    assert by_lat[38.2]["yanlis_pozitif_uzakligi"] == 0.0
    assert by_lat[38.2]["dogrulanmis_kazi_referansina_daha_yakin"] is False
    assert by_lat[38.1]["ana_esik"] is True
    assert by_lat[38.2]["mikro_bant"] is True
    assert payload["zamansal_gecersiz_dogrulanmis_kazi_referans_sayisi"] == 0

    stale_references = json.loads(json.dumps(references))
    stale_references["bolgeler"]["cesme"]["son_tarih"] = "2026-09-15"
    stale_payload = audit(morphology, stale_references, feedback)
    assert stale_payload["dogrulanmis_kazi_referans_sayisi"] == 0
    assert stale_payload["zamansal_gecersiz_dogrulanmis_kazi_referans_sayisi"] == 1
    stale_rows = stale_payload["bolgeler"]["cesme"]["adaylar"]
    assert all(row["en_yakin_dogrulanmis_kazi_id"] is None for row in stale_rows)
    assert all(
        row["dogrulanmis_kazi_referansina_daha_yakin"] is False
        for row in stale_rows
    )

    # Kullanıcının 1+4 çekirdek regresyon seti, ek tarihsel negatiflerden ayrılmalı.
    dummy_vector = [0.1] * len(FEATURES)
    core_positive_rows = [
        {"id": "FN-20260916-CESME-001", "vector": dummy_vector}
    ]
    negative_rows = [
        {"id": item_id, "vector": dummy_vector} for item_id in CORE_NEGATIVE_IDS
    ] + [{"id": "FP-EXTRA", "vector": dummy_vector}]
    core_pos, core_neg, aux_neg = _partition_core_references(
        core_positive_rows, negative_rows
    )
    assert len(core_pos) == 1
    assert len(core_neg) == 4
    assert len(aux_neg) == 1
    assert aux_neg[0]["id"] == "FP-EXTRA"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("foundation excavation reference similarity self-check: ok")
        return
    payload = audit()
    OUTPUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
