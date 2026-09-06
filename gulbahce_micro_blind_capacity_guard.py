"""Gülbahçe mikro-şantiye kalite körlüğünü kapasite açısından yorumlar.

Bu koruma mevcut ``micro_site_audit.json`` çıktısını yeniden sınıflandırır; yeni
Sentinel sorgusu yapmaz. 150-249 m² büyüklüğünde bir kör kümenin yanı sıra 250 m²+
bir kör kümenin de kendi içinde mikro ölçekli bir hafriyat/temel izini gizleyebileceğini
açıkça raporlar. Kör ceplerin yaklaşık merkezlerini de diagnostik olarak taşır ki
"aday yok" sonucu mekânsal körlükten ayrı incelenebilsin. Alarm veya saha görevi
üretmez ve ana 250 m² eşiğini değiştirmez.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


INPUT_FILE = Path(__file__).with_name("micro_site_audit.json")
OUTPUT_FILE = Path(__file__).with_name("gulbahce_micro_blind_capacity.json")
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
MAX_BLIND_EXAMPLES = 8


def _as_nonnegative_int(value):
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _blind_examples(observability, limit=MAX_BLIND_EXAMPLES):
    """Mikro izi gizleyebilecek kör cepleri koordinatlı diagnostik olarak korur."""
    rows = []
    for raw in (observability or {}).get("kor_kume_ornekleri") or []:
        if not isinstance(raw, dict):
            continue
        try:
            latitude = float(raw["enlem"])
            longitude = float(raw["boylam"])
            area = float(raw["alan_m2"])
        except (KeyError, TypeError, ValueError):
            continue
        if area < MICRO_RANGE_M2[0]:
            continue

        rounded_area = int(round(area))
        rows.append(
            {
                "enlem": round(latitude, 6),
                "boylam": round(longitude, 6),
                "alan_m2": rounded_area,
                "neden": str(raw.get("neden") or "KALITE_KORLUGU"),
                "boyut_sinifi": (
                    "MIKRO_KOR"
                    if area < MAIN_THRESHOLD_M2
                    else "ANA_KOR_MIKRO_GIZLEYEBILIR"
                ),
                "mikro_gizleyebilir": True,
                "alarm": False,
                "saha_gorevi": False,
                "harita": (
                    "https://www.google.com/maps/dir/?api=1&destination="
                    f"{latitude:.6f},{longitude:.6f}"
                ),
            }
        )

    rows.sort(
        key=lambda item: (
            0 if item["alan_m2"] < MAIN_THRESHOLD_M2 else 1,
            item["alan_m2"],
            item["enlem"],
            item["boylam"],
        )
    )
    return rows[: max(int(limit), 0)]


def build_guard(payload):
    regions = payload.get("bolgeler") or {}
    uzunkuyu = regions.get("uzunkuyu") or {}
    observability = uzunkuyu.get("gulbahce_gozlenebilirlik") or {}

    base = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": payload.get("ana_uretim_esigi_m2", MAIN_THRESHOLD_M2),
        "mikro_aralik_m2": payload.get("mikro_aralik_m2", MICRO_RANGE_M2),
        "amac": (
            "Gülbahçe'de Sentinel kalite körlüğünün 150-249 m² bir mikro şantiye izini "
            "gizleme kapasitesini, kör kümenin toplam boyutundan bağımsız olarak görünür kılmak."
        ),
    }

    if not observability:
        return {
            **base,
            "durum": "veri_yok",
            "mikro_kor_kume_tam_150_249": 0,
            "ana_kor_kume_250plus": 0,
            "mikro_gizleyebilen_kor_kume": 0,
            "mikro_korluk_kapasitesi_var": False,
            "kor_kume_ornekleri": [],
            "yorum": (
                "Gülbahçe gözlenebilirlik verisi yok; negatif sonuç üretilmedi. "
                "Bir sonraki mikro audit beklenir."
            ),
        }

    exact_micro = _as_nonnegative_int(observability.get("mikro_kor_kume_150_249"))
    main_blind = _as_nonnegative_int(observability.get("ana_kor_kume_250plus"))
    micro_capable = exact_micro + main_blind
    blind_examples = _blind_examples(observability)

    return {
        **base,
        "durum": "ok",
        "kaynak_son_tarih": uzunkuyu.get("son_tarih"),
        "kaynak_son_item": uzunkuyu.get("son_item"),
        "gulbahce_operasyon_yaricapi_m": (
            (observability.get("referans") or {}).get("operasyon_yaricapi_m")
        ),
        "kalite_kor_kara_yuzde": observability.get("kalite_kor_kara_yuzde"),
        "mikro_ham_aday_2km": _as_nonnegative_int(observability.get("mikro_ham_aday_2km")),
        "mikro_kor_kume_tam_150_249": exact_micro,
        "ana_kor_kume_250plus": main_blind,
        "mikro_gizleyebilen_kor_kume": micro_capable,
        "mikro_korluk_kapasitesi_var": micro_capable > 0,
        "kor_kume_ornekleri": blind_examples,
        "yorum": (
            "'mikro_kor_kume_tam_150_249' yalnız toplam alanı 150-249 m² olan kör kümeleri sayar. "
            "250 m²+ bir kör küme de içinde 150-249 m² yeni kazı/temel izini gizleyebilir; bu nedenle "
            "'mikro_gizleyebilen_kor_kume' iki boyut sınıfını birlikte sayar. 'kor_kume_ornekleri' "
            "yaklaşık kör küme merkezlerini yalnız diagnostik inceleme için taşır; adres/parsel değildir. "
            "Bu katman alarm veya saha görevi üretmez."
        ),
    }


def _self_check():
    assert MAIN_THRESHOLD_M2 == 250
    sample = {
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "bolgeler": {
            "uzunkuyu": {
                "son_tarih": "03.09.2026",
                "son_item": "S2_TEST",
                "gulbahce_gozlenebilirlik": {
                    "referans": {"operasyon_yaricapi_m": 2000},
                    "mikro_kor_kume_150_249": 1,
                    "ana_kor_kume_250plus": 1,
                    "mikro_ham_aday_2km": 0,
                    "kalite_kor_kara_yuzde": 0.035,
                    "kor_kume_ornekleri": [
                        {"enlem": 38.33, "boylam": 26.64, "alan_m2": 400, "neden": "BULUT"},
                        {"enlem": 38.34, "boylam": 26.65, "alan_m2": 200, "neden": "GOLGE"},
                        {"enlem": 38.35, "boylam": 26.66, "alan_m2": 100, "neden": "KALITE"},
                    ],
                },
            }
        },
    }
    result = build_guard(sample)
    assert result["durum"] == "ok"
    assert result["mikro_kor_kume_tam_150_249"] == 1
    assert result["ana_kor_kume_250plus"] == 1
    assert result["mikro_gizleyebilen_kor_kume"] == 2
    assert result["mikro_korluk_kapasitesi_var"] is True
    assert len(result["kor_kume_ornekleri"]) == 2
    assert result["kor_kume_ornekleri"][0]["alan_m2"] == 200
    assert result["kor_kume_ornekleri"][0]["boyut_sinifi"] == "MIKRO_KOR"
    assert result["kor_kume_ornekleri"][1]["boyut_sinifi"] == "ANA_KOR_MIKRO_GIZLEYEBILIR"
    assert "," in result["kor_kume_ornekleri"][0]["harita"].split("destination=")[-1]
    assert all(not item["alarm"] and not item["saha_gorevi"] for item in result["kor_kume_ornekleri"])
    assert result["alarm"] is False and result["saha_gorevi"] is False

    missing = build_guard({"ana_uretim_esigi_m2": 250, "bolgeler": {}})
    assert missing["durum"] == "veri_yok"
    assert missing["mikro_korluk_kapasitesi_var"] is False
    assert missing["kor_kume_ornekleri"] == []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Gülbahçe mikro körlük kapasitesi öz testi başarılı; 250 m² eşik değişmedi.")
        return

    if not INPUT_FILE.exists():
        raise RuntimeError("micro_site_audit.json bulunamadı.")
    payload = json.loads(INPUT_FILE.read_text(encoding="utf-8"))
    result = build_guard(payload)
    OUTPUT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Gülbahçe mikro körlük kapasitesi: "
        f"durum={result['durum']}, "
        f"tam-150-249={result['mikro_kor_kume_tam_150_249']}, "
        f"250+={result['ana_kor_kume_250plus']}, "
        f"mikro-gizleyebilen={result['mikro_gizleyebilen_kor_kume']}, "
        f"koordinatlı-örnek={len(result.get('kor_kume_ornekleri') or [])}. "
        "Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
