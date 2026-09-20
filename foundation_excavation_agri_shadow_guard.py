"""Tarla/bahçe yanlış-pozitiflerini temel-kazısı morfolojisinden ayıran gölge kalibrasyon.

Bu katman yalnız diagnostiktir; üretim eşiğini, alarmı, saha görevini veya rotayı
değiştirmez. Amaç 15-17 Eylül saha regresyonundaki dört tarla/bahçe yanlış
pozitifini güçlü negatif bağlamla bastırmanın etkisini ölçmek, mevcut müşteri
olarak doğrulanmış gerçek şantiye sinyalini ise yanlışlıkla düşürmemektir.

Sentinel-2 10 m veriden gerçek kazı derinliği ölçülmez. Buradaki gölge puan,
mevcut temel_kazi_proxy_puani üzerine iki genel ve geri alınabilir ceza uygular:
1) çekirdeğe göre geniş tarla/bahçe bağlamı, 2) güçlü çekirdek ve bitişik sınırlı
ikincil/spoil-benzeri zonun birlikte bulunmaması.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path


MORPHOLOGY_JSON = Path(__file__).with_name("foundation_excavation_morphology_review.json")
FEEDBACK_JSON = Path(__file__).with_name("manual_field_feedback.json")
OUTPUT_JSON = Path(__file__).with_name("foundation_excavation_agri_shadow_review.json")

HIGH_SCORE = 65
MEDIUM_SCORE = 45
MATCH_M = 30.0
AGRI_CONTEXT_MIN_M2 = 600.0
AGRI_CONTEXT_MIN_RATIO = 1.0
AGRI_CONTEXT_PENALTY = 30
SPARSE_EVIDENCE_PENALTY = 15

CRITICAL_TRUE_ID = "FN-20260916-CESME-001"
CRITICAL_FP_IDS = {
    "FP-20260916-REISDERE-001",
    "FP-20260917-UZUNKUYU-001",
    "FP-20260917-MUSALLA-001",
    "FP-20260917-MUSALLA-002",
}
EXISTING_CUSTOMER_ID = "OP-20260916-OVACIK-001"


def _parse_date(value):
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _distance_m(a, b):
    lat1, lon1 = float(a[0]), float(a[1])
    lat2, lon2 = float(b[0]), float(b[1])
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _level(score):
    if score >= HIGH_SCORE:
        return "YUKSEK"
    if score >= MEDIUM_SCORE:
        return "ORTA"
    return "DUSUK"


def _shadow(record):
    score = int(record.get("temel_kazi_proxy_puani") or 0)
    penalties = []

    context_area = float(record.get("tarim_baglam_alani_m2") or 0.0)
    context_ratio = float(record.get("tarim_baglam_orani") or 0.0)
    strong_fraction = float(record.get("guclu_cekirdek_orani") or 0.0)
    inner_fraction = float(record.get("yakin_cevre_degisim_orani") or 0.0)

    calibrated_agri_context = bool(
        context_area >= AGRI_CONTEXT_MIN_M2
        and context_ratio >= AGRI_CONTEXT_MIN_RATIO
    )
    if calibrated_agri_context:
        score -= AGRI_CONTEXT_PENALTY
        penalties.append("kalibrasyonlu tarla/bahçe çevre baskısı")

    sparse_evidence = bool(strong_fraction < 0.25 and inner_fraction < 0.05)
    if sparse_evidence:
        score -= SPARSE_EVIDENCE_PENALTY
        penalties.append("güçlü çekirdek ve bitişik sınırlı ikincil zon desteği yok")

    score = max(0, min(100, score))
    return {
        "ham_proxy_puani": int(record.get("temel_kazi_proxy_puani") or 0),
        "ham_proxy_seviyesi": record.get("temel_kazi_proxy_seviyesi"),
        "golge_proxy_puani": score,
        "golge_proxy_seviyesi": _level(score),
        "kalibrasyonlu_tarim_baglam_riski": calibrated_agri_context,
        "seyrek_kanit_riski": sparse_evidence,
        "golge_negatif_kanitlar": penalties,
    }


def _regression_pass(evaluated_count, high_after):
    """Boş regresyon setinin yanlışlıkla PASS sayılmasını engeller."""
    if int(evaluated_count or 0) <= 0:
        return None
    return int(high_after or 0) == 0


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _all_examples(morphology):
    out = []
    for region_key, region in (morphology.get("bolgeler") or {}).items():
        if not isinstance(region, dict):
            continue
        latest_date = _parse_date(region.get("son_tarih"))
        for record in region.get("en_yuksek_ornekler", []) or []:
            if not isinstance(record, dict):
                continue
            out.append({"bolge": region_key, "uydu_son_tarihi": latest_date, **record})
    return out


def _nearest(feedback, examples):
    target = (float(feedback["enlem"]), float(feedback["boylam"]))
    nearest = None
    nearest_distance = None
    for record in examples:
        distance = _distance_m(target, (record["enlem"], record["boylam"]))
        if nearest_distance is None or distance < nearest_distance:
            nearest = record
            nearest_distance = distance
    return nearest, nearest_distance


def _evaluate_feedback(item, examples):
    nearest, distance = _nearest(item, examples)
    result_date = _parse_date(item.get("sonuc_tarihi"))
    latest_date = nearest.get("uydu_son_tarihi") if nearest else None
    matched = bool(
        nearest is not None
        and distance is not None
        and distance <= float(item.get("eslesme_yaricapi_m") or MATCH_M)
    )

    # Yanlış-pozitif saha kararı yalnız aynı/eski uydu kanıtını bastırır. Daha yeni
    # sahne yeni bir müdahale gösterebilir; bu gölge regresyon onu kalıcı veto etmez.
    fp_temporal_valid = bool(
        str(item.get("sonuc") or "").upper() == "YANLIS_POZITIF"
        and latest_date is not None
        and result_date is not None
        and latest_date <= result_date
    )
    positive_temporal_valid = bool(
        str(item.get("sonuc") or "").upper() == "DOGRULANMIS_KAZI"
        and latest_date is not None
        and result_date is not None
        and latest_date >= result_date
    )

    shadow = _shadow(nearest) if matched else None
    return {
        "id": item.get("id"),
        "sonuc": item.get("sonuc"),
        "enlem": round(float(item["enlem"]), 6),
        "boylam": round(float(item["boylam"]), 6),
        "eslesti": matched,
        "en_yakin_mesafe_m": round(distance, 1) if distance is not None else None,
        "uydu_son_tarihi": latest_date.isoformat() if latest_date else None,
        "saha_sonuc_tarihi": result_date.isoformat() if result_date else None,
        "yanlis_pozitif_temporal_regresyon_gecerli": fp_temporal_valid,
        "pozitif_temporal_regresyon_gecerli": positive_temporal_valid,
        **(shadow or {}),
    }


def _self_check():
    agricultural = {
        "temel_kazi_proxy_puani": 90,
        "temel_kazi_proxy_seviyesi": "YUKSEK",
        "tarim_baglam_alani_m2": 1100,
        "tarim_baglam_orani": 2.0,
        "guclu_cekirdek_orani": 0.8,
        "yakin_cevre_degisim_orani": 0.2,
    }
    corrected = _shadow(agricultural)
    assert corrected["golge_proxy_puani"] == 60, corrected
    assert corrected["golge_proxy_seviyesi"] == "ORTA", corrected

    sparse = {
        "temel_kazi_proxy_puani": 65,
        "temel_kazi_proxy_seviyesi": "YUKSEK",
        "tarim_baglam_alani_m2": 0,
        "tarim_baglam_orani": 0,
        "guclu_cekirdek_orani": 0,
        "yakin_cevre_degisim_orani": 0,
    }
    corrected_sparse = _shadow(sparse)
    assert corrected_sparse["golge_proxy_puani"] == 50, corrected_sparse
    assert corrected_sparse["golge_proxy_seviyesi"] == "ORTA", corrected_sparse

    construction = {
        "temel_kazi_proxy_puani": 75,
        "temel_kazi_proxy_seviyesi": "YUKSEK",
        "tarim_baglam_alani_m2": 0,
        "tarim_baglam_orani": 0,
        "guclu_cekirdek_orani": 0,
        "yakin_cevre_degisim_orani": 0.28,
    }
    preserved = _shadow(construction)
    assert preserved["golge_proxy_puani"] == 75, preserved
    assert preserved["golge_proxy_seviyesi"] == "YUKSEK", preserved

    # En kritik regresyon-integrite testi: hiç değerlendirilen saha örneği yoksa
    # 'geçti' denemez. False yerine None kullanımı 'başarısız' ile 'ölçülemedi'yi ayırır.
    assert _regression_pass(0, 0) is None
    assert _regression_pass(2, 0) is True
    assert _regression_pass(2, 1) is False


def audit():
    _self_check()
    morphology = _load(MORPHOLOGY_JSON)
    feedback_payload = _load(FEEDBACK_JSON)
    feedback = {
        str(item.get("id")): item
        for item in feedback_payload.get("kayitlar", [])
        if isinstance(item, dict) and item.get("id")
    }
    examples = _all_examples(morphology)

    required = {CRITICAL_TRUE_ID, EXISTING_CUSTOMER_ID, *CRITICAL_FP_IDS}
    missing = sorted(required - set(feedback))
    if missing:
        raise AssertionError(f"Kalibrasyon kayıtları eksik: {missing}")

    critical_fp = [_evaluate_feedback(feedback[item_id], examples) for item_id in sorted(CRITICAL_FP_IDS)]
    true_reference = _evaluate_feedback(feedback[CRITICAL_TRUE_ID], examples)
    customer_control = _evaluate_feedback(feedback[EXISTING_CUSTOMER_ID], examples)

    evaluated_fp = [x for x in critical_fp if x["yanlis_pozitif_temporal_regresyon_gecerli"] and x["eslesti"]]
    fp_high_before = sum(x.get("ham_proxy_seviyesi") == "YUKSEK" for x in evaluated_fp)
    fp_high_after = sum(x.get("golge_proxy_seviyesi") == "YUKSEK" for x in evaluated_fp)
    matched_critical_count = sum(1 for x in critical_fp if x["eslesti"]) + int(bool(true_reference["eslesti"]))
    temporal_valid_critical_count = sum(
        1 for x in critical_fp if x["yanlis_pozitif_temporal_regresyon_gecerli"] and x["eslesti"]
    ) + int(bool(true_reference["pozitif_temporal_regresyon_gecerli"] and true_reference["eslesti"]))
    regression_pass = _regression_pass(len(evaluated_fp), fp_high_after)

    return {
        "surum": 2,
        "amac": "Kritik 15-17 Eylül regresyon setinde tarla/bahçe bağlamı için üretim-dışı gölge ceza denetimi",
        "gercek_derinlik_olcumu": False,
        "uretim_filtresi": False,
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "kritik_regresyon_seti": {
            "dogrulanmis_kazi_id": CRITICAL_TRUE_ID,
            "yanlis_pozitif_ids": sorted(CRITICAL_FP_IDS),
            "beklenen_ornek_sayisi": 5,
            "morfolojiyle_eslesen_ornek_sayisi": matched_critical_count,
            "zamansal_ve_mekansal_degerlendirilebilir_ornek_sayisi": temporal_valid_critical_count,
            "kapsam_tam": matched_critical_count == 5,
        },
        "golge_cezalari": {
            "tarim_baglam_min_m2": AGRI_CONTEXT_MIN_M2,
            "tarim_baglam_min_oran": AGRI_CONTEXT_MIN_RATIO,
            "tarim_baglam_cezasi": AGRI_CONTEXT_PENALTY,
            "seyrek_kanit_cezasi": SPARSE_EVIDENCE_PENALTY,
        },
        "kritik_yanlis_pozitifler": critical_fp,
        "degerlendirilen_kritik_fp_sayisi": len(evaluated_fp),
        "kritik_fp_yuksek_once": fp_high_before,
        "kritik_fp_yuksek_sonra": fp_high_after,
        "kritik_fp_golge_regresyon_geciyor": regression_pass,
        "kritik_fp_regresyon_degerlendirilebilir": len(evaluated_fp) > 0,
        "kritik_fp_regresyon_durumu": (
            "GECTI" if regression_pass is True else
            "KALDI" if regression_pass is False else
            "DEGERLENDIRILEMEDI"
        ),
        "dogrulanmis_kazi_referansi": true_reference,
        "mevcut_musteri_pozitif_kontrolu": customer_control,
        "not": (
            "Bu katman yalnız gölge diagnostiktir. Yanlış-pozitif kararları yalnız aynı/eski uydu sahnesi ve "
            "mekansal eşleşme varsa regresyona girer; daha yeni Sentinel kanıtı kalıcı olarak engellenmez. "
            "Hiç değerlendirilebilir örnek yoksa regresyon artık PASS sayılmaz, DEGERLENDIRILEMEDI olarak işaretlenir. "
            "Doğrulanmış kazı ise yalnız uydu tarihi saha doğrulama tarihine ulaştığında pozitif regresyona alınır."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("foundation excavation agricultural shadow self-check: ok")
        return
    payload = audit()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
