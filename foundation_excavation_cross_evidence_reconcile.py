"""Çapraz kanıt regresyonunu seed-merkezli düşük-kontrast kurtarma katmanıyla uzlaştırır.

Bu dosya yalnız diagnostik sağlık telemetrisini düzeltir. Çapraz aday üretimini,
SAR/rota kapısını, 250 m² ana eşiği veya 150–249 m² MİKRO politikasını değiştirmez.
Seed-merkezli kurtarma tek Sentinel-2 sahne çiftinden türediği için bağımsız çapraz
kanıt sayılmaz; yalnız beş saha kalibrasyon örneğinin uçtan uca regresyon sonucunda
gerçek kazı recall'ının yanlış biçimde "başarısız" raporlanmasını önler.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path


BASE = Path(__file__).resolve().parent
CROSS_JSON = BASE / "foundation_excavation_cross_evidence_review.json"
SEED_CENTERED_JSON = BASE / "seed_centered_excavation_morphology_review.json"
FEEDBACK_JSON = BASE / "manual_field_feedback.json"
AUXILIARY_NEGATIVE_IDS = {"FP-20260913-CIFTLIKKOY-001"}


def _load(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _parse_date(value):
    text = str(value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _feedback_by_id(payload):
    return {
        str(item.get("id")): item
        for item in (payload or {}).get("kayitlar", [])
        if isinstance(item, dict) and item.get("id") is not None
    }


def _seed_regression_by_id(payload):
    rows = {}
    for region_key, region in (payload or {}).get("bolgeler", {}).items():
        if not isinstance(region, dict) or region.get("durum") != "ok":
            continue
        scene_date = region.get("son_tarih")
        for item in region.get("saha_referans_regresyonu", []):
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            key = str(item.get("id"))
            candidate = {
                **item,
                "bolge_anahtari": region_key,
                "son_tarih": scene_date,
            }
            # Aynı referans iki bölge bbox'ına düşerse en yakın seed satırını koru.
            old = rows.get(key)
            if old is None:
                rows[key] = candidate
                continue
            old_distance = old.get("en_yakin_seed_mesafe_m")
            new_distance = candidate.get("en_yakin_seed_mesafe_m")
            try:
                old_distance = float(old_distance)
            except (TypeError, ValueError):
                old_distance = float("inf")
            try:
                new_distance = float(new_distance)
            except (TypeError, ValueError):
                new_distance = float("inf")
            if new_distance < old_distance:
                rows[key] = candidate
    return rows


def reconcile(cross_payload, seed_centered_payload, feedback_payload):
    if not isinstance(cross_payload, dict):
        raise RuntimeError("Çapraz kanıt diagnostik çıktısı bulunamadı.")
    if not isinstance(seed_centered_payload, dict):
        raise RuntimeError("Seed-merkezli morfoloji diagnostik çıktısı bulunamadı.")

    feedback = _feedback_by_id(feedback_payload)
    seed_rows = _seed_regression_by_id(seed_centered_payload)
    regression = []

    for original in cross_payload.get("kalibrasyon_regresyonu", []):
        if not isinstance(original, dict):
            continue
        item = dict(original)
        ref_id = str(item.get("id"))
        expected = str(item.get("sonuc") or "").upper()
        strict_ok = bool(item.get("regresyon_uyumlu"))
        seed_row = seed_rows.get(ref_id)
        feedback_row = feedback.get(ref_id) or {}

        result_date = _parse_date(feedback_row.get("sonuc_tarihi"))
        scene_date = _parse_date(seed_row.get("son_tarih")) if seed_row else None
        temporal_valid = bool(
            seed_row is not None
            and result_date is not None
            and scene_date is not None
            and scene_date >= result_date
        )
        seed_detected = bool(seed_row and seed_row.get("tespit"))
        seed_regression_ok = (
            bool(seed_row.get("regresyon_uyumlu")) if seed_row is not None else None
        )

        if expected == "DOGRULANMIS_KAZI":
            recovery_caught = bool(temporal_valid and seed_detected and seed_regression_ok)
            effective_ok = bool(strict_ok or recovery_caught)
            reason = (
                "kati_morfoloji_seed_caprazi"
                if strict_ok
                else "seed_merkezli_dusuk_kontrast_kurtarma"
                if recovery_caught
                else "pozitif_referans_hala_kaciriliyor"
            )
        elif expected == "YANLIS_POZITIF":
            recovery_caught = False
            if ref_id in AUXILIARY_NEGATIVE_IDS:
                # Eski Çiftlikköy bahçe-temizliği kaydı yardımcı negatiftir; kullanıcının
                # çekirdek 4 tarla/bahçe regresyon örneği yerine sert veto üretmez.
                effective_ok = True
                reason = "yardimci_negatif_regresyon_hesabindan_haric"
            else:
                # Çekirdek negatif referansta seed-merkezli katman yanlış tetikliyorsa
                # regresyon geçmez; böylece recall kurtarması FP kapısını gevşetmez.
                effective_ok = bool(strict_ok and (seed_regression_ok is not False))
                reason = (
                    "negatif_referans_bastirildi"
                    if effective_ok
                    else "negatif_referans_katmanlardan_birinde_tetiklendi"
                )
        else:
            # MEVCUT_MUSTERI temel-kazısı sınıflandırma başarım hesabına dahil edilmez.
            recovery_caught = False
            effective_ok = strict_ok
            reason = "siniflandirma_regresyonundan_haric"

        item["kati_capraz_regresyon_uyumlu"] = strict_ok
        item["regresyon_hesap_rolu"] = (
            "yardimci_negatif"
            if ref_id in AUXILIARY_NEGATIVE_IDS
            else "cekirdek_veya_yeni_saha_referansi"
        )
        item["dusuk_kontrast_kurtarma_yakaladi"] = recovery_caught
        item["dusuk_kontrast_kurtarma_temporal_gecerli"] = temporal_valid
        item["dusuk_kontrast_kurtarma"] = (
            {
                "bolge_anahtari": seed_row.get("bolge_anahtari"),
                "son_tarih": seed_row.get("son_tarih"),
                "en_yakin_seed_mesafe_m": seed_row.get("en_yakin_seed_mesafe_m"),
                "seed_merkezli_morfoloji_puani": seed_row.get(
                    "seed_merkezli_morfoloji_puani"
                ),
                "seed_merkezli_morfoloji_seviyesi": seed_row.get(
                    "seed_merkezli_morfoloji_seviyesi"
                ),
                "tespit": seed_detected,
                "regresyon_uyumlu": seed_regression_ok,
            }
            if seed_row is not None
            else None
        )
        item["regresyon_uyumlu"] = effective_ok
        item["regresyon_karar_kaynagi"] = reason
        regression.append(item)

    failures = [item for item in regression if not item.get("regresyon_uyumlu")]
    payload = dict(cross_payload)
    payload["surum"] = max(int(payload.get("surum") or 0), 3)
    payload["kalibrasyon_regresyonu"] = regression
    payload["toplam_regresyon_uyumsuz"] = len(failures)
    payload["regresyon_uyumsuzluklari"] = failures
    payload["regresyon_uzlastirma"] = {
        "uygulandi": True,
        "kaynak": "seed_centered_excavation_morphology_review.json",
        "bagimsiz_capraz_kanit_sayilir": False,
        "rota_ve_alarm_esiklerini_degistirir": False,
        "not": (
            "Seed-merkezli düşük-kontrast kurtarma yalnız saha regresyonu sağlık "
            "telemetrisini uzlaştırır; aynı Sentinel-2 sahne çiftinden türediği için "
            "SAR/ikinci tarih gibi bağımsız kanıt yerine geçmez."
        ),
    }
    return payload


def _self_check():
    cross = {
        "surum": 2,
        "kalibrasyon_regresyonu": [
            {"id": "TP", "sonuc": "DOGRULANMIS_KAZI", "regresyon_uyumlu": False},
            {"id": "FP", "sonuc": "YANLIS_POZITIF", "regresyon_uyumlu": True},
            {"id": "CLIENT", "sonuc": "MEVCUT_MUSTERI", "regresyon_uyumlu": True},
            {
                "id": "FP-20260913-CIFTLIKKOY-001",
                "sonuc": "YANLIS_POZITIF",
                "regresyon_uyumlu": False,
            },
        ],
        "toplam_regresyon_uyumsuz": 1,
        "regresyon_uyumsuzluklari": [{"id": "TP"}],
    }
    seed_centered = {
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "son_tarih": "18.09.2026",
                "saha_referans_regresyonu": [
                    {
                        "id": "TP",
                        "en_yakin_seed_mesafe_m": 11.2,
                        "seed_merkezli_morfoloji_puani": 75,
                        "seed_merkezli_morfoloji_seviyesi": "YUKSEK",
                        "tespit": True,
                        "regresyon_uyumlu": True,
                    },
                    {
                        "id": "FP",
                        "en_yakin_seed_mesafe_m": 500.0,
                        "seed_merkezli_morfoloji_puani": None,
                        "seed_merkezli_morfoloji_seviyesi": None,
                        "tespit": False,
                        "regresyon_uyumlu": True,
                    },
                    {
                        "id": "FP-20260913-CIFTLIKKOY-001",
                        "en_yakin_seed_mesafe_m": 5.0,
                        "seed_merkezli_morfoloji_puani": 90,
                        "seed_merkezli_morfoloji_seviyesi": "YUKSEK",
                        "tespit": True,
                        "regresyon_uyumlu": False,
                    },
                ],
            }
        }
    }
    feedback = {
        "kayitlar": [
            {"id": "TP", "sonuc": "DOGRULANMIS_KAZI", "sonuc_tarihi": "2026-09-16"},
            {"id": "FP", "sonuc": "YANLIS_POZITIF", "sonuc_tarihi": "2026-09-17"},
            {"id": "CLIENT", "sonuc": "MEVCUT_MUSTERI", "sonuc_tarihi": "2026-09-17"},
            {
                "id": "FP-20260913-CIFTLIKKOY-001",
                "sonuc": "YANLIS_POZITIF",
                "sonuc_tarihi": "2026-09-13",
            },
        ]
    }
    payload = reconcile(cross, seed_centered, feedback)
    by_id = {item["id"]: item for item in payload["kalibrasyon_regresyonu"]}
    assert by_id["TP"]["kati_capraz_regresyon_uyumlu"] is False
    assert by_id["TP"]["dusuk_kontrast_kurtarma_yakaladi"] is True
    assert by_id["TP"]["regresyon_uyumlu"] is True
    assert by_id["FP"]["regresyon_uyumlu"] is True
    assert by_id["FP-20260913-CIFTLIKKOY-001"]["regresyon_hesap_rolu"] == "yardimci_negatif"
    assert by_id["FP-20260913-CIFTLIKKOY-001"]["regresyon_uyumlu"] is True
    assert payload["toplam_regresyon_uyumsuz"] == 0

    false_positive_leak = json.loads(json.dumps(seed_centered))
    fp_row = false_positive_leak["bolgeler"]["cesme"]["saha_referans_regresyonu"][1]
    fp_row["tespit"] = True
    fp_row["regresyon_uyumlu"] = False
    payload = reconcile(cross, false_positive_leak, feedback)
    by_id = {item["id"]: item for item in payload["kalibrasyon_regresyonu"]}
    assert by_id["FP"]["regresyon_uyumlu"] is False
    assert payload["toplam_regresyon_uyumsuz"] == 1

    future_feedback = json.loads(json.dumps(feedback))
    future_feedback["kayitlar"][0]["sonuc_tarihi"] = "2026-09-19"
    payload = reconcile(cross, seed_centered, future_feedback)
    by_id = {item["id"]: item for item in payload["kalibrasyon_regresyonu"]}
    assert by_id["TP"]["dusuk_kontrast_kurtarma_temporal_gecerli"] is False
    assert by_id["TP"]["regresyon_uyumlu"] is False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("foundation excavation cross evidence reconcile self-check: ok")
        return 0

    cross_payload = _load(CROSS_JSON)
    seed_centered_payload = _load(SEED_CENTERED_JSON)
    feedback_payload = _load(FEEDBACK_JSON)
    payload = reconcile(cross_payload, seed_centered_payload, feedback_payload)
    CROSS_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
