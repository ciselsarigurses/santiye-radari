"""150-249 m² mikro adaylarda yerel değişim ile geniş-yüzey hareketini ayırır.

Mikro zaman-serisi desteği tek başına yeterli değildir: tarla sürümü veya toplu
arazi temizliği aynı Sentinel geçişinde birçok küçük, kompakt parçayı ani başlangıç
gibi gösterebilir. Bu diagnostik katman her adayın yaklaşık 3x3 merkez yamasındaki
en güçlü pikselleri, çevresindeki 9x9 bağlam halkasıyla karşılaştırır.

Amaç yalnız önceliklendirme kanıtı üretmektir. Alarm/saha görevi üretmez, ana
250 m² eşiğini değiştirmez ve tarla/sit/tarım statüsünü kalıcı eleme nedeni yapmaz.
Gülbahçe ayrı kapsama referansı olarak raporlanmaya devam eder.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

import satellite
import micro_site_temporal_guard as temporal


TEMPORAL_FILE = Path(__file__).with_name("micro_site_temporal_review.json")
OUTPUT_FILE = Path(__file__).with_name("micro_site_locality_review.json")

CENTER_RADIUS_PIXELS = 1
CONTEXT_RADIUS_PIXELS = 4
MIN_CONTEXT_VALID_FRACTION = 0.55
CONTEXT_ACTIVE_SCORE_MIN = 0.30
BROAD_CONTEXT_Q75_MIN = 0.18
BROAD_CONTEXT_ACTIVE_FRACTION_MIN = 0.25
BROAD_LOCAL_CONTRAST_MAX = 2.0
LOCAL_COMPACT_CONTRAST_MIN = 2.5
LOCAL_COMPACT_TARGET_SCORE_MIN = 0.45
INSUFFICIENT_CONTEXT_LABEL = "YETERSIZ_BAGLAM_VERISI"


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _window_slices(row, column, shape, radius):
    height, width = shape
    return (
        slice(max(0, row - radius), min(height, row + radius + 1)),
        slice(max(0, column - radius), min(width, column + radius + 1)),
    )


def _score_map(older, latest):
    older_rgb, older_ndvi, older_brightness = older
    latest_rgb, latest_ndvi, latest_brightness = latest
    rgb_change = np.mean(np.abs(latest_rgb - older_rgb), axis=2)
    ndvi_loss = older_ndvi - latest_ndvi
    brightness_gain = latest_brightness - older_brightness
    return (
        np.maximum(rgb_change, 0.0)
        + 0.7 * np.maximum(ndvi_loss, 0.0)
        + 0.3 * np.maximum(brightness_gain, 0.0)
    )


def _locality_metrics(score, valid, row, column, pixel_count):
    row_slice, col_slice = _window_slices(
        row, column, valid.shape, CONTEXT_RADIUS_PIXELS
    )
    local_score = score[row_slice, col_slice]
    local_valid = valid[row_slice, col_slice]
    height, width = local_valid.shape

    center_row = row - (row_slice.start or 0)
    center_col = column - (col_slice.start or 0)
    rr, cc = np.ogrid[:height, :width]
    center_mask = (
        (np.abs(rr - center_row) <= CENTER_RADIUS_PIXELS)
        & (np.abs(cc - center_col) <= CENTER_RADIUS_PIXELS)
    )
    center_valid = local_valid & center_mask
    context_valid = local_valid & ~center_mask

    center_values = local_score[center_valid]
    context_values = local_score[context_valid]
    if center_values.size == 0 or context_values.size == 0:
        return None

    k = max(1, min(int(pixel_count or 2), int(center_values.size)))
    target_values = np.sort(center_values)[-k:]
    target_score = float(np.mean(target_values))
    context_q75 = float(np.quantile(context_values, 0.75))
    context_mean = float(np.mean(context_values))
    context_active_fraction = float(
        np.mean(context_values >= CONTEXT_ACTIVE_SCORE_MIN)
    )
    context_valid_fraction = float(
        context_values.size / max(int((~center_mask).sum()), 1)
    )
    local_contrast = target_score / max(context_q75, 0.05)

    broad_risk = bool(
        context_valid_fraction >= MIN_CONTEXT_VALID_FRACTION
        and context_q75 >= BROAD_CONTEXT_Q75_MIN
        and context_active_fraction >= BROAD_CONTEXT_ACTIVE_FRACTION_MIN
        and local_contrast <= BROAD_LOCAL_CONTRAST_MAX
    )
    compact_support = bool(
        context_valid_fraction >= MIN_CONTEXT_VALID_FRACTION
        and target_score >= LOCAL_COMPACT_TARGET_SCORE_MIN
        and local_contrast >= LOCAL_COMPACT_CONTRAST_MIN
        and not broad_risk
    )
    return {
        "context_valid_fraction": context_valid_fraction,
        "target_score": target_score,
        "context_q75": context_q75,
        "context_mean": context_mean,
        "context_active_fraction": context_active_fraction,
        "local_contrast": local_contrast,
        "broad_risk": broad_risk,
        "compact_support": compact_support,
    }


def _locality_label(metrics, temporal_support):
    if metrics is None:
        return INSUFFICIENT_CONTEXT_LABEL, False, False, True

    context_valid_fraction = _number(metrics.get("context_valid_fraction"), 0.0)
    if context_valid_fraction < MIN_CONTEXT_VALID_FRACTION:
        # Çevrenin çoğunu göremiyorsak "lokal değil" veya "geniş hareket" diyemeyiz.
        # Adayı negatif sınıfa itmek yerine sonraki açık Sentinel sahnesine bırak.
        return INSUFFICIENT_CONTEXT_LABEL, False, False, True

    broad_risk = bool(metrics.get("broad_risk"))
    compact_support = bool(metrics.get("compact_support"))
    if broad_risk:
        label = "GENIS_YUZEY_KONTEKST_RISKI"
    elif compact_support and temporal_support:
        label = "LOKAL_KOMPAKT_TEMPORAL_DESTEK"
    elif compact_support:
        label = "LOKAL_KOMPAKT_DESTEK"
    else:
        label = "NÖTR_BAGLAM"
    return label, broad_risk, compact_support, False


def _analyze_region(region_key, region_payload):
    rows = [
        dict(item) for item in (region_payload.get("adaylar") or [])
        if isinstance(item, dict)
    ]
    if not rows:
        return {
            "durum": "ok",
            "olculen_aday": 0,
            "temporal_destekli_aday": 0,
            "genis_yuzey_kontekst_riski": 0,
            "lokal_kompakt_destek": 0,
            "yetersiz_baglam_verisi": 0,
            "gulbahce_olculen": 0,
            "adaylar": [],
        }

    bbox = satellite.REGIONS[region_key]["bbox"]
    items = satellite._search_items(bbox)
    older = temporal._find_item(items, region_payload.get("onceki_item"))
    latest = temporal._find_item(items, region_payload.get("son_item"))
    if older is None or latest is None:
        return {
            "durum": "atlandi",
            "neden": "temporal_kaynak_sentinel_cifti_bulunamadi",
            "aday_sayisi": len(rows),
        }

    height, width = satellite._output_shape(bbox)
    older_rgb, older_ndvi, older_brightness, older_scl = temporal._scene_arrays(
        older, bbox, height, width
    )
    latest_rgb, latest_ndvi, latest_brightness, latest_scl = temporal._scene_arrays(
        latest, bbox, height, width
    )
    valid = ~np.isin(older_scl, satellite.EXCLUDED_SCL_CLASSES)
    valid &= ~np.isin(latest_scl, satellite.EXCLUDED_SCL_CLASSES)
    water = (older_scl == 6) | (latest_scl == 6)
    valid &= ~satellite._dilate_mask(water, satellite.COASTAL_WATER_BUFFER_PIXELS)
    score = _score_map(
        (older_rgb, older_ndvi, older_brightness),
        (latest_rgb, latest_ndvi, latest_brightness),
    )

    analyzed = []
    for raw in rows:
        try:
            latitude = float(raw["enlem"])
            longitude = float(raw["boylam"])
        except (KeyError, TypeError, ValueError):
            continue

        row, column = temporal._pixel_for_point(
            latitude, longitude, bbox, valid.shape
        )
        metrics = _locality_metrics(
            score, valid, row, column, int(_number(raw.get("piksel"), 2))
        )
        item = dict(raw)
        temporal_support = bool(
            item.get("ani_baslangic_destegi")
            or item.get("devam_eden_hareket_destegi")
        )
        label, broad_risk, compact_support, reimage = _locality_label(
            metrics, temporal_support
        )

        if metrics is None:
            item.update(
                {
                    "baglam_gecerli_oran": 0.0,
                    "hedef_degisim_skoru": None,
                    "cevre_q75_degisim_skoru": None,
                    "cevre_ortalama_degisim_skoru": None,
                    "cevre_aktif_piksel_orani": None,
                    "yerel_kontrast_orani": None,
                    "genis_yuzey_kontekst_riski": False,
                    "lokal_kompakt_destek": False,
                    "lokalite_sinifi": label,
                    "baglam_yeniden_goruntule": reimage,
                }
            )
        else:
            item.update(
                {
                    "baglam_gecerli_oran": round(metrics["context_valid_fraction"], 3),
                    "hedef_degisim_skoru": round(metrics["target_score"], 4),
                    "cevre_q75_degisim_skoru": round(metrics["context_q75"], 4),
                    "cevre_ortalama_degisim_skoru": round(metrics["context_mean"], 4),
                    "cevre_aktif_piksel_orani": round(metrics["context_active_fraction"], 3),
                    "yerel_kontrast_orani": round(metrics["local_contrast"], 2),
                    "genis_yuzey_kontekst_riski": broad_risk,
                    "lokal_kompakt_destek": compact_support,
                    "lokalite_sinifi": label,
                    "baglam_yeniden_goruntule": reimage,
                }
            )
        analyzed.append(item)

    analyzed.sort(
        key=lambda item: (
            bool(item.get("genis_yuzey_kontekst_riski")),
            not bool(item.get("lokal_kompakt_destek")),
            not bool(item.get("baglam_yeniden_goruntule")),
            not bool(item.get("ani_baslangic_destegi")),
            -_number(item.get("yerel_kontrast_orani"), 0.0),
            -_number(item.get("hedef_degisim_skoru"), 0.0),
        )
    )
    return {
        "durum": "ok",
        "bolge": satellite.REGIONS[region_key]["label"],
        "onceki_item": older.get("id"),
        "onceki_tarih": satellite._item_date(older),
        "son_item": latest.get("id"),
        "son_tarih": satellite._item_date(latest),
        "ornekleme": "3x3_hedef_topK_vs_9x9_cevre_halkasi",
        "olculen_aday": len(analyzed),
        "temporal_destekli_aday": sum(
            bool(item.get("ani_baslangic_destegi") or item.get("devam_eden_hareket_destegi"))
            for item in analyzed
        ),
        "genis_yuzey_kontekst_riski": sum(
            bool(item.get("genis_yuzey_kontekst_riski")) for item in analyzed
        ),
        "lokal_kompakt_destek": sum(
            bool(item.get("lokal_kompakt_destek")) for item in analyzed
        ),
        "yetersiz_baglam_verisi": sum(
            str(item.get("lokalite_sinifi") or "") == INSUFFICIENT_CONTEXT_LABEL
            for item in analyzed
        ),
        "gulbahce_olculen": sum(bool(item.get("gulbahce_cevre")) for item in analyzed),
        "adaylar": analyzed,
    }


def _self_check():
    assert satellite.MIN_HOTSPOT_AREA_M2 == 250
    assert CENTER_RADIUS_PIXELS < CONTEXT_RADIUS_PIXELS

    score = np.zeros((9, 9), dtype="float32")
    valid = np.ones((9, 9), dtype=bool)
    score[4, 4] = 0.9
    score[4, 5] = 0.8
    compact = _locality_metrics(score, valid, 4, 4, 2)
    assert compact is not None
    assert compact["compact_support"]
    assert not compact["broad_risk"]
    label, broad_risk, compact_support, reimage = _locality_label(compact, True)
    assert label == "LOKAL_KOMPAKT_TEMPORAL_DESTEK"
    assert compact_support and not broad_risk and not reimage

    broad = np.full((9, 9), 0.4, dtype="float32")
    broad[4, 4] = 0.65
    broad[4, 5] = 0.6
    broad_metrics = _locality_metrics(broad, valid, 4, 4, 2)
    assert broad_metrics is not None
    assert broad_metrics["broad_risk"]
    assert not broad_metrics["compact_support"]

    # Geçerli çevre %55'in altındaysa ölçülen q75/kontrast negatif kanıt olamaz.
    partial_valid = np.zeros((9, 9), dtype=bool)
    partial_valid[3:6, 3:6] = True
    partial_valid[0:2, 0:6] = True
    partial = _locality_metrics(score, partial_valid, 4, 4, 2)
    assert partial is not None
    assert partial["context_valid_fraction"] < MIN_CONTEXT_VALID_FRACTION
    label, broad_risk, compact_support, reimage = _locality_label(partial, True)
    assert label == INSUFFICIENT_CONTEXT_LABEL
    assert not broad_risk and not compact_support and reimage


def run_review():
    _self_check()
    if not TEMPORAL_FILE.exists():
        raise RuntimeError("micro_site_temporal_review.json bulunamadı.")
    source = json.loads(TEMPORAL_FILE.read_text(encoding="utf-8"))
    payload = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": satellite.MIN_HOTSPOT_AREA_M2,
        "mikro_aralik_m2": source.get("mikro_aralik_m2", [150, 249]),
        "kaynak_temporal_olusturma": source.get("olusturma"),
        "amac": (
            "Mikro temporal sinyalin yalnız aday çevresinde mi yoğunlaştığını, yoksa "
            "aynı Sentinel geçişinde çevrede de yaygın olup tarla/toprak temizliği gibi "
            "geniş-yüzey hareketi riski taşıyıp taşımadığını diagnostik olarak ölçmek."
        ),
        "esikler": {
            "merkez_yaricap_piksel": CENTER_RADIUS_PIXELS,
            "baglam_yaricap_piksel": CONTEXT_RADIUS_PIXELS,
            "minimum_baglam_gecerli_oran": MIN_CONTEXT_VALID_FRACTION,
            "cevre_aktif_skor_min": CONTEXT_ACTIVE_SCORE_MIN,
            "genis_yuzey_cevre_q75_min": BROAD_CONTEXT_Q75_MIN,
            "genis_yuzey_aktif_oran_min": BROAD_CONTEXT_ACTIVE_FRACTION_MIN,
            "genis_yuzey_yerel_kontrast_max": BROAD_LOCAL_CONTRAST_MAX,
            "lokal_kompakt_kontrast_min": LOCAL_COMPACT_CONTRAST_MIN,
            "lokal_kompakt_hedef_skor_min": LOCAL_COMPACT_TARGET_SCORE_MIN,
        },
        "uyari": (
            "Bu çıktı alarm veya saha görevi üretmez. Geniş-yüzey riski tarla/sit/tarım "
            "statüsü anlamına gelmez ve adayı kalıcı olarak silmez. Çevre bağlamının "
            "%55'ten azı geçerliyse aday negatif sayılmaz; YETERSIZ_BAGLAM_VERISI olarak "
            "arka planda tutulur ve sonraki açık Sentinel sahnesinde yeniden ölçülür."
        ),
        "bolgeler": {},
    }

    for region_key in ("cesme", "uzunkuyu"):
        region_payload = (source.get("bolgeler") or {}).get(region_key) or {}
        if region_payload.get("durum") != "ok":
            payload["bolgeler"][region_key] = {
                "durum": "atlandi",
                "neden": "temporal_kaynak_bolge_ok_degil",
                "aday_sayisi": int(region_payload.get("olculen_aday") or 0),
            }
            continue
        try:
            payload["bolgeler"][region_key] = _analyze_region(
                region_key, region_payload
            )
        except Exception as exc:
            payload["bolgeler"][region_key] = {
                "durum": "hata",
                "neden": f"{type(exc).__name__}: {exc}",
                "aday_sayisi": int(region_payload.get("olculen_aday") or 0),
            }

    payload["toplam"] = {
        key: sum(
            int(data.get(key) or 0)
            for data in payload["bolgeler"].values()
            if isinstance(data, dict)
        )
        for key in (
            "olculen_aday",
            "temporal_destekli_aday",
            "genis_yuzey_kontekst_riski",
            "lokal_kompakt_destek",
            "yetersiz_baglam_verisi",
            "gulbahce_olculen",
        )
    }
    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Mikro lokalite öz testi başarılı; alarm/görev/eşik değişmedi.")
        return

    payload = run_review()
    total = payload.get("toplam") or {}
    print(
        "Mikro lokalite incelemesi tamamlandı: "
        f"ölçülen={int(total.get('olculen_aday') or 0)}, "
        f"temporal={int(total.get('temporal_destekli_aday') or 0)}, "
        f"geniş-yüzey-risk={int(total.get('genis_yuzey_kontekst_riski') or 0)}, "
        f"lokal-kompakt={int(total.get('lokal_kompakt_destek') or 0)}, "
        f"yetersiz-bağlam={int(total.get('yetersiz_baglam_verisi') or 0)}, "
        f"Gülbahçe={int(total.get('gulbahce_olculen') or 0)}. "
        "Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
