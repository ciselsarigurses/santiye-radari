"""Gülbahçe kör alan devriyesine yakın ikinci kör kümeleri diagnostik olarak işaretler.

Amaç, seçilmiş saha devriyesine çok yakın olan 250 m²+ güncel-sahne kör kümelerini
aynı saha ziyareti sırasında kısa bir ek bakışla değerlendirebilmektir. Bu katman
35 m'lik kesin "devriye kapsıyor" semantiğini gevşetmez; yalnız 35-150 m arası
kümeleri fırsat niteliğinde ek kontrol olarak işaretler.

Alarm veya saha görevi üretmez. 250 m² ana üretim eşiğini ve 150-249 m² MİKRO
politikasını değiştirmez. Erişilebilirlik sahada değerlendirilir.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


INPUT_JSON = Path(__file__).with_name("gulbahce_latest_state_blind_review.json")
MATCH_RADIUS_M = 35
NEARBY_RADIUS_M = 150
MAIN_THRESHOLD_M2 = 250


def _distance(item):
    try:
        value = item.get("secilen_devriyeye_mesafe_m")
        return float(value) if value is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def augment(payload):
    """Yakın ek kontrolleri ekler; mevcut kapsama/alarm/görev alanlarına dokunmaz."""
    result = dict(payload or {})
    rows = []
    nearby_count = 0
    nearby_area = 0

    for item in (result.get("guncel_sahne_kor_kumeleri") or []):
        row = dict(item)
        distance = _distance(row)
        covered = bool(row.get("secilen_devriye_kapsiyor"))
        try:
            area = int(row.get("alan_m2") or 0)
        except (TypeError, ValueError):
            area = 0

        nearby = bool(
            area >= MAIN_THRESHOLD_M2
            and not covered
            and distance is not None
            and MATCH_RADIUS_M < distance <= NEARBY_RADIUS_M
        )
        row["secilen_devriyeye_yakin_ek_kontrol"] = nearby
        if nearby:
            row["yakin_ek_kontrol_notu"] = (
                "Seçilen Gülbahçe kör alan devriyesine 150 m içinde; kapsanmış sayılmaz. "
                "Saha erişimi uygunsa aynı ziyarette kısa ek kontrol yapılabilir."
            )
            nearby_count += 1
            nearby_area += area
        rows.append(row)

    result["guncel_sahne_kor_kumeleri"] = rows
    result["secilen_devriye_kapsama_yaricapi_m"] = MATCH_RADIUS_M
    result["yakin_ek_kontrol_yaricapi_m"] = NEARBY_RADIUS_M
    result["secilen_devriyeye_yakin_ek_kontrol_sayisi"] = nearby_count
    result["secilen_devriyeye_yakin_ek_kontrol_alan_m2"] = nearby_area
    result["yakin_ek_kontrol_yorumu"] = (
        "Yakın ek kontrol yalnız 250 m²+ güncel-sahne kör kümeleri için saha verimliliği "
        "diagnostigidir. 'Devriye kapsıyor' sayılmaz, alarm/görev üretmez ve erişilebilirlik "
        "sahada değerlendirilir."
    )
    return result


def _self_check():
    fixture = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": 250,
        "mikro_santiye_araligi_m2": [150, 249],
        "guncel_sahne_kor_kumeleri": [
            {
                "enlem": 38.341406,
                "boylam": 26.643308,
                "alan_m2": 1200,
                "secilen_devriyeye_mesafe_m": 0,
                "secilen_devriye_kapsiyor": True,
                "alarm": False,
                "saha_gorevi": False,
            },
            {
                "enlem": 38.342310,
                "boylam": 26.642621,
                "alan_m2": 400,
                "secilen_devriyeye_mesafe_m": 117,
                "secilen_devriye_kapsiyor": False,
                "alarm": False,
                "saha_gorevi": False,
            },
            {
                "enlem": 38.346923,
                "boylam": 26.641132,
                "alan_m2": 1600,
                "secilen_devriyeye_mesafe_m": 639,
                "secilen_devriye_kapsiyor": False,
                "alarm": False,
                "saha_gorevi": False,
            },
        ],
    }
    result = augment(fixture)
    rows = result["guncel_sahne_kor_kumeleri"]
    assert rows[0]["secilen_devriyeye_yakin_ek_kontrol"] is False
    assert rows[1]["secilen_devriyeye_yakin_ek_kontrol"] is True
    assert rows[2]["secilen_devriyeye_yakin_ek_kontrol"] is False
    assert rows[1]["secilen_devriye_kapsiyor"] is False
    assert result["secilen_devriyeye_yakin_ek_kontrol_sayisi"] == 1
    assert result["secilen_devriyeye_yakin_ek_kontrol_alan_m2"] == 400
    assert result["alarm"] is False and result["saha_gorevi"] is False
    assert result["ana_uretim_esigi_m2"] == 250
    assert result["mikro_santiye_araligi_m2"] == [150, 249]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if args.check_only:
        print(
            "Gülbahçe yakın kör alan ek kontrol öz testi başarılı; 35 m kesin kapsama "
            "korundu, 35-150 m yalnız diagnostik, alarm/görev yok."
        )
        return

    if not INPUT_JSON.exists():
        raise RuntimeError(f"Gerekli girdi bulunamadı: {INPUT_JSON.name}")

    payload = json.loads(INPUT_JSON.read_text(encoding="utf-8"))
    result = augment(payload)
    INPUT_JSON.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Gülbahçe yakın ek kör alan kontrolü: "
        f"adet={result['secilen_devriyeye_yakin_ek_kontrol_sayisi']}, "
        f"alan={result['secilen_devriyeye_yakin_ek_kontrol_alan_m2']} m². "
        "Kesin kapsama değişmedi; alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
