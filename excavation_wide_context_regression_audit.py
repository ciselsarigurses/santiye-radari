"""1 gerçek kazı + 4 tarla/bahçe yanlış pozitifte geniş bağlam ayrımını ölçer.

Yalnız diagnostiktir. Alarm, saha görevi, rota, 250 m² ana eşik veya 150–249 m²
MİKRO politikası değiştirmez. Sentinel-2 10 m veriden gerçek kazı derinliği
ölçüldüğü iddia edilmez.

Amaç, lokal seed/morfoloji katmanının doygunlaştığı durumda 5x5 çekirdeği 15x15
çevreyle karşılaştırarak geniş-planar tarla hareketi ile lokal kepçe/temel müdahalesi
arasındaki ayrım için test edilebilir negatif bağlam özellikleri üretmektir.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

import excavation_reference_signature_audit as signature
import satellite


OUTPUT_JSON = Path(__file__).with_name("excavation_wide_context_regression_review.json")
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]

CALIBRATION = [
    {"id": "GERCEK_KAZI", "sonuc": "DOGRULANMIS_KAZI", "enlem": 38.341846, "boylam": 26.432861},
    {"id": "FP_REISDERE", "sonuc": "YANLIS_POZITIF", "enlem": 38.322140, "boylam": 26.407912},
    {"id": "FP_UZUNKUYU", "sonuc": "YANLIS_POZITIF", "enlem": 38.320150, "boylam": 26.562138},
    {"id": "FP_MUSALLA_1", "sonuc": "YANLIS_POZITIF", "enlem": 38.313186, "boylam": 26.307059},
    {"id": "FP_MUSALLA_2", "sonuc": "YANLIS_POZITIF", "enlem": 38.308120, "boylam": 26.326863},
]


def _window(array, row, col, radius):
    r0 = max(row - radius, 0)
    r1 = min(row + radius + 1, array.shape[0])
    c0 = max(col - radius, 0)
    c1 = min(col + radius + 1, array.shape[1])
    return array[r0:r1, c0:c1]


def _fraction(mask, valid):
    denominator = int(np.count_nonzero(valid))
    if denominator <= 0:
        return 0.0
    return float(np.count_nonzero(mask & valid) / denominator)


def _mean(values, valid):
    selected = values[valid]
    return float(np.mean(selected)) if selected.size else 0.0


def _metrics(arrays, latitude, longitude):
    row, col = signature._row_col(latitude, longitude, arrays["bbox"], arrays["shape"])

    valid5 = _window(arrays["valid"], row, col, 2)
    valid15 = _window(arrays["valid"], row, col, 7)
    rgb5 = _window(arrays["rgb_difference"], row, col, 2)
    rgb15 = _window(arrays["rgb_difference"], row, col, 7)
    brightness15 = _window(arrays["brightness_gain"], row, col, 7)
    ndvi15 = _window(arrays["latest_ndvi"], row, col, 7)
    veg_loss15 = _window(arrays["vegetation_loss"], row, col, 7)

    mean5 = _mean(rgb5, valid5)
    mean15 = _mean(rgb15, valid15)
    changed5 = _fraction(rgb5 >= 0.10, valid5)
    changed15 = _fraction(rgb15 >= 0.10, valid15)
    bright15 = _fraction(brightness15 > 0.035, valid15)
    dark15 = _fraction(brightness15 < -0.025, valid15)
    bare15 = _fraction(ndvi15 < 0.30, valid15)
    persistent_veg15 = _fraction(ndvi15 > 0.35, valid15)
    veg_loss15_fraction = _fraction(veg_loss15 > 0.14, valid15)

    localization = mean5 / max(mean15, 0.01)
    changed_localization = changed5 / max(changed15, 0.01)

    # Yalnız kalibrasyon hipotezi: geniş 15x15 değişim + düşük lokalizasyon,
    # parsel ölçeğinde planar/tarla hareketiyle daha uyumludur. Üretim filtresi değildir.
    broad_planar_proxy = bool(changed15 >= 0.30 and localization <= 1.35)

    return {
        "ortalama_rgb_5x5": round(mean5, 4),
        "ortalama_rgb_15x15": round(mean15, 4),
        "rgb_lokalizasyon_5_15": round(localization, 3),
        "degisen_piksel_orani_5x5": round(changed5, 4),
        "degisen_piksel_orani_15x15": round(changed15, 4),
        "degisim_lokalizasyon_5_15": round(changed_localization, 3),
        "parlak_artis_orani_15x15": round(bright15, 4),
        "koyulasma_orani_15x15": round(dark15, 4),
        "ciplak_zemin_orani_15x15": round(bare15, 4),
        "kalici_bitki_orani_15x15": round(persistent_veg15, 4),
        "bitki_kaybi_orani_15x15": round(veg_loss15_fraction, 4),
        "genis_planar_negatif_proxy": broad_planar_proxy,
    }


def _point_in_bbox(point, bbox):
    west, south, east, north = bbox
    return south <= point["enlem"] <= north and west <= point["boylam"] <= east


def _self_check():
    shape = (31, 31)
    valid = np.ones(shape, dtype=bool)
    base = {
        "valid": valid,
        "brightness_gain": np.zeros(shape, dtype=float),
        "latest_ndvi": np.full(shape, 0.2, dtype=float),
        "vegetation_loss": np.zeros(shape, dtype=float),
        "bbox": [26.0, 38.0, 26.31, 38.31],
        "shape": shape,
    }

    local = np.full(shape, 0.02, dtype=float)
    local[14:17, 14:17] = 0.30
    broad = np.full(shape, 0.15, dtype=float)

    # Merkez pikseli WGS84 dönüşüm hatasından bağımsız test etmek için doğrudan
    # aynı pencere matematiğini kullan.
    def synthetic_metrics(rgb):
        rgb5 = _window(rgb, 15, 15, 2)
        rgb15 = _window(rgb, 15, 15, 7)
        v5 = _window(valid, 15, 15, 2)
        v15 = _window(valid, 15, 15, 7)
        mean5 = _mean(rgb5, v5)
        mean15 = _mean(rgb15, v15)
        return mean5 / max(mean15, 0.01), _fraction(rgb15 >= 0.10, v15)

    local_ratio, local_broad_fraction = synthetic_metrics(local)
    broad_ratio, broad_broad_fraction = synthetic_metrics(broad)
    assert local_ratio > broad_ratio, (local_ratio, broad_ratio)
    assert local_broad_fraction < broad_broad_fraction, (local_broad_fraction, broad_broad_fraction)
    assert len(CALIBRATION) == 5
    assert sum(x["sonuc"] == "DOGRULANMIS_KAZI" for x in CALIBRATION) == 1


def audit():
    _self_check()
    regions = {}
    rows = []
    for region_key in ("cesme", "uzunkuyu"):
        arrays = signature._region_arrays(region_key)
        region_rows = []
        for point in CALIBRATION:
            if not _point_in_bbox(point, arrays["bbox"]):
                continue
            measured = {
                **point,
                "bolge_anahtari": region_key,
                **_metrics(arrays, point["enlem"], point["boylam"]),
            }
            region_rows.append(measured)
            rows.append(measured)
        regions[region_key] = {
            "bolge": satellite.REGIONS[region_key]["label"],
            "onceki_tarih": arrays["older_date"],
            "son_tarih": arrays["latest_date"],
            "kalibrasyonlar": region_rows,
        }

    # Aynı kalibrasyon noktasının örtüşen AOI'lerde iki kez ölçülmesi halinde bir
    # tanesini seç. Şu anki beş nokta pratikte tek AOI'ye düşer; bu koruma sessiz
    # çift sayımı önler.
    unique = {}
    for row in rows:
        unique.setdefault(row["id"], row)
    rows = [unique[p["id"]] for p in CALIBRATION if p["id"] in unique]

    true_rows = [x for x in rows if x["sonuc"] == "DOGRULANMIS_KAZI"]
    fp_rows = [x for x in rows if x["sonuc"] == "YANLIS_POZITIF"]
    true_localization = true_rows[0]["rgb_lokalizasyon_5_15"] if true_rows else None
    fp_localizations = [x["rgb_lokalizasyon_5_15"] for x in fp_rows]

    return {
        "surum": 1,
        "amac": "Gerçek kazı ile tarla/bahçe yanlış pozitiflerini 5x5 çekirdek ve 15x15 çevre bağlamında karşılaştırmak",
        "gercek_derinlik_olcumu": False,
        "uretim_filtresi": False,
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "kalibrasyon_beklenen_sayi": 5,
        "kalibrasyon_olculen_sayi": len(rows),
        "kalibrasyonlar": rows,
        "ayirt_edilebilirlik": {
            "gercek_kazi_rgb_lokalizasyon_5_15": true_localization,
            "yanlis_pozitif_rgb_lokalizasyon_5_15": fp_localizations,
            "gercek_kazi_tum_fp_uzerinde": bool(
                true_localization is not None
                and fp_localizations
                and true_localization > max(fp_localizations)
            ),
            "genis_planar_proxy_fp_sayisi": sum(
                bool(x["genis_planar_negatif_proxy"]) for x in fp_rows
            ),
            "genis_planar_proxy_gercek_kazi": bool(
                true_rows and true_rows[0]["genis_planar_negatif_proxy"]
            ),
        },
        "bolgeler": regions,
        "not": (
            "Bu çıktı eşik veya rota kararı değildir. Geniş-planar negatif proxy yalnız beş saha örneğinde "
            "kalibrasyon hipotezidir; yeni saha doğrulamaları olmadan üretime bağlanmaz."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("excavation wide-context regression self-check: ok")
        return
    payload = audit()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
