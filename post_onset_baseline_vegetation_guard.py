"""13 Eylül baseline referans-benzeri adaylarında korunmuş bitki dokusunu bastırır.

Bu katman yalnız diagnostiktir. Ana 250 m² eşiğini, 150–249 m² MİKRO politikasını,
alarmı veya saha rotasını değiştirmez. Amaç, doğrulanmış kazı referansında görülmeyen
çoğunluk kalıcı bitki/ağaç dokusunu güçlü tarla-bahçe negatif bağlamı olarak işaretlemek
ve referans-benzeri kısa listede yanlış güven artışını önlemektir.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


BASE = Path(__file__).resolve().parent
INPUT = BASE / "post_onset_baseline_excavation_review.json"
OUTPUT = BASE / "post_onset_baseline_vegetation_guard_review.json"
MAX_PERSISTENT_VEG_FRACTION = 0.50


def _load(path=INPUT):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Baseline kazı diagnostik çıktısı geçersiz.")
    return payload


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _vegetation_negative(item):
    return _number(item.get("kalici_bitki_orani_5x5")) >= MAX_PERSISTENT_VEG_FRACTION


def _region_guard(region):
    candidates = [
        item for item in (region.get("benzer_diagnostik_adaylar") or [])
        if isinstance(item, dict)
    ]
    kept = []
    suppressed = []
    for item in candidates:
        row = dict(item)
        row["kalici_bitki_negatif_baglami"] = _vegetation_negative(row)
        if row["kalici_bitki_negatif_baglami"]:
            row["negatif_baglamsal_neden"] = (
                "5x5 çevrede kalıcı bitki/ağaç dokusu çoğunlukta; tarla/bahçe lehine güçlü negatif bağlam."
            )
            row["alarm"] = False
            row["saha_gorevi"] = False
            suppressed.append(row)
        else:
            kept.append(row)

    critical = [
        item for item in (region.get("kritik_regresyon") or [])
        if isinstance(item, dict)
    ]
    true_rows = [x for x in critical if str(x.get("sonuc") or "").upper() == "DOGRULANMIS_KAZI"]
    true_reference_safe = all(not _vegetation_negative(x) for x in true_rows)

    return {
        "durum": region.get("durum"),
        "bolge": region.get("bolge"),
        "baseline_tarih": region.get("baseline_tarih"),
        "post_onset_tarih": region.get("post_onset_tarih"),
        "girdi_aday_sayisi": len(candidates),
        "korunan_aday_sayisi": len(kept),
        "kalici_bitki_bastirilan_sayi": len(suppressed),
        "gercek_kazi_referansi_korundu": true_reference_safe,
        "korunan_adaylar": kept,
        "bastirilan_adaylar": suppressed,
        "alarm": False,
        "saha_gorevi": False,
    }


def audit(payload=None):
    payload = payload or _load()
    regions = {}
    for key, region in (payload.get("bolgeler") or {}).items():
        if not isinstance(region, dict):
            continue
        regions[key] = _region_guard(region)

    suppressed = sum(int(x.get("kalici_bitki_bastirilan_sayi") or 0) for x in regions.values())
    true_safe = all(x.get("gercek_kazi_referansi_korundu") is not False for x in regions.values())
    return {
        "surum": 1,
        "amac": "13 Eylül baseline referans-benzeri adaylarında çoğunluk korunmuş bitki dokusunu tarla/bahçe negatif bağlamı olarak bastırmak",
        "gercek_derinlik_olcumu": False,
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "kalici_bitki_negatif_esigi": MAX_PERSISTENT_VEG_FRACTION,
        "toplam_kalici_bitki_bastirilan": suppressed,
        "gercek_kazi_referansi_korundu": true_safe,
        "uretim_filtresi": False,
        "alarm": False,
        "saha_gorevi": False,
        "bolgeler": regions,
        "not": (
            "Bu çıktı üretim veya rota filtresi değildir. Kalıcı bitki oranı >=0.50 olan referans-benzeri "
            "adayları güçlü tarla/bahçe negatif bağlamı olarak ayırır; doğrulanmış gerçek kazı referansının "
            "bastırılmadığını regresyon güvenliği olarak raporlar."
        ),
    }


def _self_check():
    assert _vegetation_negative({"kalici_bitki_orani_5x5": 0.50})
    assert _vegetation_negative({"kalici_bitki_orani_5x5": 0.72})
    assert not _vegetation_negative({"kalici_bitki_orani_5x5": 0.49})
    sample = {
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "bolge": "Çeşme",
                "kritik_regresyon": [
                    {"sonuc": "DOGRULANMIS_KAZI", "kalici_bitki_orani_5x5": 0.0}
                ],
                "benzer_diagnostik_adaylar": [
                    {"enlem": 1, "boylam": 1, "kalici_bitki_orani_5x5": 0.72},
                    {"enlem": 2, "boylam": 2, "kalici_bitki_orani_5x5": 0.12},
                ],
            }
        }
    }
    result = audit(sample)
    assert result["toplam_kalici_bitki_bastirilan"] == 1
    assert result["gercek_kazi_referansi_korundu"] is True
    assert result["bolgeler"]["cesme"]["korunan_aday_sayisi"] == 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("post-onset baseline vegetation guard self-check: ok")
        return 0
    payload = audit()
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
