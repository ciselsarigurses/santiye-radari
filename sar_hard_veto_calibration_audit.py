"""Sentinel-1 desteğinin temel/kepçe adaylarında zorunlu veto olarak kullanılıp kullanılamayacağını denetler.

Bu katman yalnız diagnostiktir. Alarm, saha görevi veya rota üretmez; 250 m² ana eşik
ve 150–249 m² MİKRO politikası değişmez. Amaç, saha doğrulanmış gerçek kazıların
zamansal olarak geçerli SAR ölçümünde güçlü-lokal destek alamadığı durumda SAR'ı
"yoksa kazı değildir" biçiminde sert veto olarak kullanmayı fail-closed engellemektir.

SAR yine bağımsız pozitif kanıt olarak değerlidir; bu denetim yalnız negatif/veto
kullanımının kalibrasyonla desteklenip desteklenmediğini ölçer.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


BASE = Path(__file__).resolve().parent
SAR_REVIEW = BASE / "foundation_excavation_sar_seed_review.json"
FEEDBACK_JSON = BASE / "manual_field_feedback.json"
OUTPUT_JSON = BASE / "sar_hard_veto_calibration_review.json"

POSITIVE_RESULT = "DOGRULANMIS_KAZI"
MIN_CONFIRMED_FOR_HARD_VETO_CALIBRATION = 2


def _load(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _confirmed_ids(feedback):
    return {
        str(item.get("id"))
        for item in (feedback.get("kayitlar") or [])
        if isinstance(item, dict)
        and item.get("id")
        and str(item.get("sonuc") or "").upper() == POSITIVE_RESULT
    }


def _calibration_rows(sar_review, confirmed_ids):
    rows = []
    for region_key, region in (sar_review.get("bolgeler") or {}).items():
        if not isinstance(region, dict):
            continue
        for item in region.get("saha_kalibrasyon_sonuclari") or []:
            if not isinstance(item, dict):
                continue
            calibration_id = str(item.get("kalibrasyon_id") or "")
            if calibration_id not in confirmed_ids:
                continue
            # Yalnız saha doğrulama tarihine yetişmiş SAR sahnesi, gerçek pozitif
            # üzerinde zorunlu-veto kalibrasyonu için gözlem sayılabilir.
            if item.get("sar_saha_dogrulama_sonrasi") is not True:
                continue
            rows.append({
                "bolge_anahtari": region_key,
                "kalibrasyon_id": calibration_id,
                "enlem": item.get("enlem"),
                "boylam": item.get("boylam"),
                "sar_lokal_degisim_skor_db": item.get("sar_lokal_degisim_skor_db"),
                "sar_polarizasyon_uyumu": item.get("sar_polarizasyon_uyumu"),
                "sar_mekansal_ayrim": item.get("sar_mekansal_ayrim"),
                "sar_guclu_lokal_destek": item.get("sar_guclu_lokal_destek") is True,
                "sar_kalibrasyon_destegi": item.get("sar_kalibrasyon_destegi") is True,
            })
    return rows


def audit(sar_review=None, feedback=None):
    sar_review = sar_review if isinstance(sar_review, dict) else _load(SAR_REVIEW)
    feedback = feedback if isinstance(feedback, dict) else _load(FEEDBACK_JSON)
    confirmed_ids = _confirmed_ids(feedback)
    rows = _calibration_rows(sar_review, confirmed_ids)
    missed = [row for row in rows if not row["sar_kalibrasyon_destegi"]]
    supported = [row for row in rows if row["sar_kalibrasyon_destegi"]]

    enough_references = len(rows) >= MIN_CONFIRMED_FOR_HARD_VETO_CALIBRATION
    all_supported = bool(rows) and not missed
    hard_veto_calibrated = bool(enough_references and all_supported)
    hard_veto_unsafe_observed = bool(missed)

    return {
        "surum": 1,
        "amac": "SAR yokluğunu temel/kepçe kazısı için zorunlu negatif veto yapmadan önce saha pozitifleriyle kalibre etmek",
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "zorunlu_veto_kalibrasyonu_icin_min_dogrulanmis_kazi": MIN_CONFIRMED_FOR_HARD_VETO_CALIBRATION,
        "zamansal_gecerli_sar_kalibrasyon_pozitifi": len(rows),
        "sar_destekli_dogrulanmis_kazi": len(supported),
        "sar_tarafindan_kacirilan_dogrulanmis_kazi": len(missed),
        "sar_zorunlu_veto_kalibre": hard_veto_calibrated,
        "sar_zorunlu_veto_guvenli_degil_gozlendi": hard_veto_unsafe_observed,
        "sar_zorunlu_veto_kullanilabilir": hard_veto_calibrated and not hard_veto_unsafe_observed,
        "kalibrasyon_sonuclari": rows,
        "kacirilan_pozitifler": missed,
        "politika": (
            "SAR güçlü-lokal desteği bağımsız pozitif kanıt olarak kullanılabilir; ancak en az iki zamansal geçerli "
            "saha-doğrulanmış kazının tamamı SAR tarafından desteklenmeden SAR yokluğu negatif/hard-veto değildir. "
            "Doğrulanmış bir kazı SAR tarafından kaçırılırsa zorunlu veto açıkça güvensiz kabul edilir."
        ),
    }


def _self_check():
    feedback = {
        "kayitlar": [
            {"id": "TP1", "sonuc": POSITIVE_RESULT},
            {"id": "TP2", "sonuc": POSITIVE_RESULT},
            {"id": "FP1", "sonuc": "YANLIS_POZITIF"},
        ]
    }
    sar = {
        "bolgeler": {
            "cesme": {
                "saha_kalibrasyon_sonuclari": [
                    {
                        "kalibrasyon_id": "TP1",
                        "enlem": 38.30,
                        "boylam": 26.30,
                        "sar_saha_dogrulama_sonrasi": True,
                        "sar_guclu_lokal_destek": False,
                        "sar_kalibrasyon_destegi": False,
                        "sar_lokal_degisim_skor_db": 0.8,
                    },
                    {
                        "kalibrasyon_id": "TP2",
                        "enlem": 38.31,
                        "boylam": 26.31,
                        "sar_saha_dogrulama_sonrasi": True,
                        "sar_guclu_lokal_destek": True,
                        "sar_kalibrasyon_destegi": True,
                        "sar_lokal_degisim_skor_db": 2.4,
                    },
                    {
                        "kalibrasyon_id": "FP1",
                        "sar_saha_dogrulama_sonrasi": True,
                        "sar_kalibrasyon_destegi": True,
                    },
                ]
            }
        }
    }
    payload = audit(sar, feedback)
    assert payload["zamansal_gecerli_sar_kalibrasyon_pozitifi"] == 2, payload
    assert payload["sar_tarafindan_kacirilan_dogrulanmis_kazi"] == 1, payload
    assert payload["sar_zorunlu_veto_guvenli_degil_gozlendi"] is True, payload
    assert payload["sar_zorunlu_veto_kullanilabilir"] is False, payload

    sar["bolgeler"]["cesme"]["saha_kalibrasyon_sonuclari"][0]["sar_kalibrasyon_destegi"] = True
    payload = audit(sar, feedback)
    assert payload["sar_tarafindan_kacirilan_dogrulanmis_kazi"] == 0, payload
    assert payload["sar_zorunlu_veto_kalibre"] is True, payload
    assert payload["sar_zorunlu_veto_kullanilabilir"] is True, payload

    one_reference_feedback = {"kayitlar": [{"id": "TP1", "sonuc": POSITIVE_RESULT}]}
    payload = audit(sar, one_reference_feedback)
    assert payload["zamansal_gecerli_sar_kalibrasyon_pozitifi"] == 1, payload
    assert payload["sar_zorunlu_veto_kalibre"] is False, payload
    assert payload["sar_zorunlu_veto_kullanilabilir"] is False, payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("SAR hard-veto calibration self-check: ok")
        return
    payload = audit()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
