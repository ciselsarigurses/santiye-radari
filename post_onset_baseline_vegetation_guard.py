"""13 Eylül baseline referans-benzeri adaylarında korunmuş bitki dokusunu bastırır.

Bu katman yalnız diagnostiktir. Ana 250 m² eşiğini, 150–249 m² MİKRO politikasını,
alarmı veya saha rotasını değiştirmez. Amaç, doğrulanmış kazı referansında görülmeyen
çoğunluk kalıcı bitki/ağaç dokusunu ve Musalla yanlış-pozitifine benzeyen karma
bitki+çıplak-zemin dokusunu güçlü tarla-bahçe negatif bağlamı olarak işaretlemek,
referans-benzeri kısa listede yanlış güven artışını önlemektir.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


BASE = Path(__file__).resolve().parent
INPUT = BASE / "post_onset_baseline_excavation_review.json"
OUTPUT = BASE / "post_onset_baseline_vegetation_guard_review.json"
MAX_PERSISTENT_VEG_FRACTION = 0.50
MIXED_PERSISTENT_VEG_MIN = 0.40
MIXED_BARE_GROUND_MAX = 0.60


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
    persistent = _number(item.get("kalici_bitki_orani_5x5"))
    bare = _number(item.get("ciplak_zemin_orani_5x5"))
    majority_vegetation = persistent >= MAX_PERSISTENT_VEG_FRACTION
    mixed_field_texture = (
        persistent >= MIXED_PERSISTENT_VEG_MIN
        and bare <= MIXED_BARE_GROUND_MAX
    )
    return majority_vegetation or mixed_field_texture


def _negative_reason(item):
    persistent = _number(item.get("kalici_bitki_orani_5x5"))
    bare = _number(item.get("ciplak_zemin_orani_5x5"))
    if persistent >= MAX_PERSISTENT_VEG_FRACTION:
        return (
            "5x5 çevrede kalıcı bitki/ağaç dokusu çoğunlukta; tarla/bahçe lehine güçlü negatif bağlam."
        )
    if persistent >= MIXED_PERSISTENT_VEG_MIN and bare <= MIXED_BARE_GROUND_MAX:
        return (
            "5x5 çevrede kalıcı bitki dokusu korunurken çıplak zemin çoğunluk oluşturmuyor; "
            "Musalla tarla/bahçe yanlış-pozitif imzasına benzeyen karma arazi bağlamı."
        )
    return None


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
            row["negatif_baglamsal_neden"] = _negative_reason(row)
            row["alarm"] = False
            row["saha_gorevi"] = False
            suppressed.append(row)
        else:
            kept.append(row)

    critical = [
        item for item in (region.get("kritik_regresyon") or [])
        if isinstance(item, dict)
    ]
    true_rows = [
        x for x in critical
        if str(x.get("sonuc") or "").upper() == "DOGRULANMIS_KAZI"
    ]
    # all([]) Python'da True döner. Gerçek-kazı referansı bu bölgenin dışında ise
    # bölgesel korumayı başarı gibi raporlamak yanlış güven üretir. Böyle bölgelerde
    # durum "uygulanamaz" (None) olmalı; yalnız gerçekten referans içeren bölge
    # regresyon güvenliğini belirler.
    true_reference_safe = (
        all(not _vegetation_negative(x) for x in true_rows)
        if true_rows
        else None
    )

    return {
        "durum": region.get("durum"),
        "bolge": region.get("bolge"),
        "baseline_tarih": region.get("baseline_tarih"),
        "post_onset_tarih": region.get("post_onset_tarih"),
        "girdi_aday_sayisi": len(candidates),
        "korunan_aday_sayisi": len(kept),
        "kalici_bitki_bastirilan_sayi": len(suppressed),
        "gercek_kazi_referansi_sayisi": len(true_rows),
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

    suppressed = sum(
        int(x.get("kalici_bitki_bastirilan_sayi") or 0)
        for x in regions.values()
    )
    true_reference_regions = [
        x for x in regions.values()
        if int(x.get("gercek_kazi_referansi_sayisi") or 0) > 0
    ]
    true_reference_count = sum(
        int(x.get("gercek_kazi_referansi_sayisi") or 0)
        for x in true_reference_regions
    )
    true_safe = bool(true_reference_regions) and all(
        x.get("gercek_kazi_referansi_korundu") is True
        for x in true_reference_regions
    )
    return {
        "surum": 3,
        "amac": "13 Eylül baseline referans-benzeri adaylarında korunmuş/karma bitki dokusunu tarla-bahçe negatif bağlamı olarak bastırmak",
        "gercek_derinlik_olcumu": False,
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "kalici_bitki_negatif_esigi": MAX_PERSISTENT_VEG_FRACTION,
        "karma_bitki_min_esigi": MIXED_PERSISTENT_VEG_MIN,
        "karma_ciplak_zemin_max_esigi": MIXED_BARE_GROUND_MAX,
        "toplam_kalici_bitki_bastirilan": suppressed,
        "gercek_kazi_referansi_sayisi": true_reference_count,
        "gercek_kazi_referansi_korundu": true_safe,
        "uretim_filtresi": False,
        "alarm": False,
        "saha_gorevi": False,
        "bolgeler": regions,
        "not": (
            "Bu çıktı üretim veya rota filtresi değildir. Kalıcı bitki oranı >=0.50 olan veya "
            "kalıcı bitki >=0.40 iken çıplak zemin <=0.60 kalan referans-benzeri adayları güçlü "
            "tarla/bahçe negatif bağlamı olarak ayırır. İkinci kural, kritik Musalla yanlış-pozitifinin "
            "0.40 kalıcı-bitki / 0.52 çıplak-zemin imzasını hedefler; doğrulanmış gerçek kazı referansı "
            "0.00 / 0.92 olduğu için korunur. Referans içermeyen bölgelerde bölgesel koruma durumu "
            "null/uygulanamaz olarak bırakılır."
        ),
    }


def _self_check():
    assert _vegetation_negative({"kalici_bitki_orani_5x5": 0.50, "ciplak_zemin_orani_5x5": 0.80})
    assert _vegetation_negative({"kalici_bitki_orani_5x5": 0.72, "ciplak_zemin_orani_5x5": 0.20})
    # Kritik Musalla yanlış-pozitif imzası: çoğunluk bitki değil ama karma tarla/bahçe dokusu.
    assert _vegetation_negative({"kalici_bitki_orani_5x5": 0.40, "ciplak_zemin_orani_5x5": 0.52})
    assert not _vegetation_negative({"kalici_bitki_orani_5x5": 0.39, "ciplak_zemin_orani_5x5": 0.52})
    assert not _vegetation_negative({"kalici_bitki_orani_5x5": 0.40, "ciplak_zemin_orani_5x5": 0.61})
    # Doğrulanmış gerçek kazı baseline→post-onset imzası korunmalı.
    assert not _vegetation_negative({"kalici_bitki_orani_5x5": 0.0, "ciplak_zemin_orani_5x5": 0.92})
    sample = {
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "bolge": "Çeşme",
                "kritik_regresyon": [
                    {
                        "sonuc": "DOGRULANMIS_KAZI",
                        "kalici_bitki_orani_5x5": 0.0,
                        "ciplak_zemin_orani_5x5": 0.92,
                    }
                ],
                "benzer_diagnostik_adaylar": [
                    {"enlem": 1, "boylam": 1, "kalici_bitki_orani_5x5": 0.72, "ciplak_zemin_orani_5x5": 0.20},
                    {"enlem": 2, "boylam": 2, "kalici_bitki_orani_5x5": 0.40, "ciplak_zemin_orani_5x5": 0.52},
                    {"enlem": 3, "boylam": 3, "kalici_bitki_orani_5x5": 0.12, "ciplak_zemin_orani_5x5": 0.84},
                ],
            },
            "uzunkuyu": {
                "durum": "ok",
                "bolge": "Uzunkuyu",
                "kritik_regresyon": [
                    {"sonuc": "YANLIS_POZITIF", "kalici_bitki_orani_5x5": 0.72, "ciplak_zemin_orani_5x5": 0.20}
                ],
                "benzer_diagnostik_adaylar": [],
            },
        }
    }
    result = audit(sample)
    assert result["toplam_kalici_bitki_bastirilan"] == 2
    assert result["gercek_kazi_referansi_sayisi"] == 1
    assert result["gercek_kazi_referansi_korundu"] is True
    assert result["bolgeler"]["cesme"]["gercek_kazi_referansi_sayisi"] == 1
    assert result["bolgeler"]["cesme"]["gercek_kazi_referansi_korundu"] is True
    assert result["bolgeler"]["cesme"]["korunan_aday_sayisi"] == 1
    assert result["bolgeler"]["uzunkuyu"]["gercek_kazi_referansi_sayisi"] == 0
    assert result["bolgeler"]["uzunkuyu"]["gercek_kazi_referansi_korundu"] is None


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
