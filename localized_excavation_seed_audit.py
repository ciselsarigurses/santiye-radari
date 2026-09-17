"""Gerçek kazı referansındaki lokal/seyrek Sentinel imzasını diagnostik olarak arar.

17 Eylül saha kalibrasyonu, mevcut ana maskenin tarla/bahçe yüzey değişimlerini
fazla geniş yakalarken doğrulanmış kazıda yalnız çok lokal birkaç pikselin değiştiğini
gösterdi. Bu katman o farkı ölçer; alarm/görev üretmez ve 250 m² ana eşiğini
DEĞİŞTİRMEZ.

Kural yalnız bir doğrulanmış pozitif örnekten türetildiği için üretim filtresine
bağlanması yasaktır. Amaç yeni saha örnekleri geldikçe test edilebilir bir regresyon
hipotezi oluşturmaktır.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

import excavation_reference_signature_audit as signature
import satellite


OUTPUT_JSON = Path(__file__).with_name("localized_excavation_seed_review.json")
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]

# 13→15 Eylül saha doğrulamalarından ölçülen, geniş tarla değişimine karşı
# lokal kazı çekirdeğini seçmek için temkinli diagnostik sınırlar.
MAX_LOCAL_MEAN_RGB = 0.09
MAX_CONTEXT_MEAN_RGB = 0.06
MIN_LOCAL_MAX_RGB = 0.20
MIN_BRIGHT_FRACTION = 0.08
MAX_BRIGHT_FRACTION = 0.50
MIN_SOIL_FRACTION = 0.01
MAX_SOIL_FRACTION = 0.08
MAX_DARK_FRACTION = 0.35


def _safe_fraction(mask, valid):
    count = int(np.count_nonzero(valid))
    if count <= 0:
        return 0.0
    return float(np.count_nonzero(mask & valid) / count)


def _window_metrics(arrays, row, col):
    valid5 = signature._window(arrays["valid"], row, col, 2)
    valid9 = signature._window(arrays["valid"], row, col, 4)
    rgb5 = signature._window(arrays["rgb_difference"], row, col, 2)
    rgb9 = signature._window(arrays["rgb_difference"], row, col, 4)
    bg5 = signature._window(arrays["brightness_gain"], row, col, 2)
    soil5 = signature._window(arrays["current_soil"], row, col, 2)

    rgb5_values = rgb5[valid5]
    rgb9_values = rgb9[valid9]
    bg5_values = bg5[valid5]
    valid5_count = max(int(np.count_nonzero(valid5)), 1)

    return {
        "ortalama_rgb_5x5": round(float(np.mean(rgb5_values)) if rgb5_values.size else 0.0, 4),
        "max_rgb_5x5": round(float(np.max(rgb5_values)) if rgb5_values.size else 0.0, 4),
        "ortalama_rgb_9x9": round(float(np.mean(rgb9_values)) if rgb9_values.size else 0.0, 4),
        "parlak_artis_orani_5x5": round(float(np.count_nonzero(bg5_values > 0.035) / valid5_count), 4),
        "koyulasma_orani_5x5": round(float(np.count_nonzero(bg5_values < -0.025) / valid5_count), 4),
        "soil_mask_orani_5x5": round(_safe_fraction(soil5, valid5), 4),
    }


def _passes(metrics):
    return bool(
        metrics["max_rgb_5x5"] >= MIN_LOCAL_MAX_RGB
        and metrics["ortalama_rgb_5x5"] <= MAX_LOCAL_MEAN_RGB
        and metrics["ortalama_rgb_9x9"] <= MAX_CONTEXT_MEAN_RGB
        and MIN_BRIGHT_FRACTION <= metrics["parlak_artis_orani_5x5"] <= MAX_BRIGHT_FRACTION
        and MIN_SOIL_FRACTION <= metrics["soil_mask_orani_5x5"] <= MAX_SOIL_FRACTION
        and metrics["koyulasma_orani_5x5"] <= MAX_DARK_FRACTION
    )


def _location(row, col, bbox, shape):
    height, width = shape
    west, south, east, north = bbox
    latitude = north - (row + 0.5) / height * (north - south)
    longitude = west + (col + 0.5) / width * (east - west)
    return round(latitude, 6), round(longitude, 6)


def _reference_checks(region_key, arrays):
    checks = []
    for item in signature._load_feedback():
        if not signature._point_in_bbox(item, arrays["bbox"]):
            continue
        row, col = signature._row_col(
            float(item["enlem"]),
            float(item["boylam"]),
            arrays["bbox"],
            arrays["shape"],
        )
        metrics = _window_metrics(arrays, row, col)
        detected = _passes(metrics)
        expected_positive = str(item.get("sonuc") or "").upper() == "DOGRULANMIS_KAZI"
        checks.append(
            {
                "id": item.get("id"),
                "sonuc": str(item.get("sonuc") or "").upper(),
                "enlem": round(float(item["enlem"]), 6),
                "boylam": round(float(item["boylam"]), 6),
                "lokal_kazi_seed_uyumu": detected,
                "regresyon_uyumlu": detected == expected_positive,
                **metrics,
            }
        )
    return checks


def _discover(region_key, arrays):
    # Seed pikselleri geniş/planar değişimi değil, kuvvetli ve parlak lokal değişimi
    # başlatır. Nihai karar komşuluk seyrekliğine göre verilir.
    seed = (
        arrays["valid"]
        & (arrays["rgb_difference"] >= MIN_LOCAL_MAX_RGB)
        & (arrays["brightness_gain"] > 0.035)
        & (arrays["latest_ndvi"] < 0.35)
    )
    candidates = []
    for component in satellite._connected_components(seed):
        pixels = np.asarray(component, dtype="int32")
        values = arrays["rgb_difference"][pixels[:, 0], pixels[:, 1]]
        best = pixels[int(np.argmax(values))]
        row, col = int(best[0]), int(best[1])
        metrics = _window_metrics(arrays, row, col)
        if not _passes(metrics):
            continue
        latitude, longitude = _location(row, col, arrays["bbox"], arrays["shape"])
        candidates.append(
            {
                "enlem": latitude,
                "boylam": longitude,
                "seed_piksel": len(component),
                "spektral_etki_alani_m2": len(component) * 100,
                "alarm": False,
                "saha_gorevi": False,
                "neden_diagnostik": (
                    "Kuvvetli değişim 5x5/9x9 çevrede seyrek ve lokal; tarla düzeltmesi gibi "
                    "geniş homojen değişim göstermiyor. Alan tahmini kazı alanı değildir."
                ),
                **metrics,
            }
        )
    candidates.sort(key=lambda x: (-x["max_rgb_5x5"], x["ortalama_rgb_5x5"]))
    return candidates


def _self_check():
    true_like = {
        "ortalama_rgb_5x5": 0.0629,
        "max_rgb_5x5": 0.298,
        "ortalama_rgb_9x9": 0.0482,
        "parlak_artis_orani_5x5": 0.4,
        "koyulasma_orani_5x5": 0.12,
        "soil_mask_orani_5x5": 0.04,
    }
    reisdere = dict(true_like, ortalama_rgb_5x5=0.1176, soil_mask_orani_5x5=0.16)
    musalla_bright = dict(true_like, ortalama_rgb_5x5=0.1576, parlak_artis_orani_5x5=0.96)
    musalla_dark = dict(true_like, ortalama_rgb_5x5=0.1944, parlak_artis_orani_5x5=0.0, soil_mask_orani_5x5=0.0)
    uzunkuyu = dict(true_like, max_rgb_5x5=0.1725, soil_mask_orani_5x5=0.20)
    assert _passes(true_like)
    assert not _passes(reisdere)
    assert not _passes(musalla_bright)
    assert not _passes(musalla_dark)
    assert not _passes(uzunkuyu)


def audit():
    _self_check()
    regions = {}
    all_failures = []
    for region_key in ("cesme", "uzunkuyu"):
        try:
            arrays = signature._region_arrays(region_key)
            checks = _reference_checks(region_key, arrays)
            failures = [item for item in checks if not item["regresyon_uyumlu"]]
            all_failures.extend(
                [{"bolge_anahtari": region_key, **item} for item in failures]
            )
            regions[region_key] = {
                "bolge": satellite.REGIONS[region_key]["label"],
                "durum": "ok",
                "onceki_tarih": arrays["older_date"],
                "son_tarih": arrays["latest_date"],
                "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
                "mikro_aralik_m2": MICRO_RANGE_M2,
                "alarm": False,
                "saha_gorevi": False,
                "saha_referans_regresyonu": checks,
                "regresyon_uyumsuz_sayisi": len(failures),
                "lokal_seed_diagnostik_adaylari": _discover(region_key, arrays)[:20],
            }
        except Exception as exc:
            regions[region_key] = {
                "bolge": satellite.REGIONS[region_key]["label"],
                "durum": "hata",
                "hata": f"{type(exc).__name__}: {exc}",
            }
    return {
        "surum": 1,
        "amac": "Geniş tarla yüzey değişiminden farklı, lokal/seyrek temel kazısı seed hipotezini test etmek",
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "esikler": {
            "max_ortalama_rgb_5x5": MAX_LOCAL_MEAN_RGB,
            "max_ortalama_rgb_9x9": MAX_CONTEXT_MEAN_RGB,
            "min_max_rgb_5x5": MIN_LOCAL_MAX_RGB,
            "parlak_artis_orani_5x5": [MIN_BRIGHT_FRACTION, MAX_BRIGHT_FRACTION],
            "soil_mask_orani_5x5": [MIN_SOIL_FRACTION, MAX_SOIL_FRACTION],
            "max_koyulasma_orani_5x5": MAX_DARK_FRACTION,
        },
        "toplam_regresyon_uyumsuz": len(all_failures),
        "regresyon_uyumsuzluklari": all_failures,
        "bolgeler": regions,
        "not": (
            "Bir doğrulanmış gerçek kazı ve saha yanlış-pozitiflerinden türetilmiş ilk hipotezdir. "
            "Yeni saha doğrulamaları, temporal devamlılık ve Sentinel-1 desteği olmadan rotaya bağlanmaz."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("localized excavation seed self-check: ok")
        return
    payload = audit()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
