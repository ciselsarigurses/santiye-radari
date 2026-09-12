"""Sentinel-1 RTC ile optik kor alanlarda lokal geri-sacilim degisimini olcer.

Bu katman yalniz diagnostiktir. Tek basina insaat/kazi alarmi veya saha gorevi
uretmez. Ayni goreli yorunge + orbit yonu + polarizasyon imzasina sahip iki
Sentinel-1 RTC sahnesinde, optik kor alan hedefinin merkezindeki kucuk pencere
ile yakin cevre halkasi arasindaki kontrastin zamansal degisimini olcer.

Lokal kontrast kullanilmasinin nedeni yagis/nem gibi sahne-geneli degisimlerin
bir kismini bastirip kompakt/lokal mudahaleyi daha ayrik izleyebilmektir.
VV/VH birlikte varsa operasyonel siralama icin iki polarizasyonun daha zayif
olanini konservatif skor olarak kullanir; tek-polarizasyon baskin sicramalari
ayri diagnostik sinifta tutar. Esikler ham kalibrasyon esikleridir; saha geri
bildirimi olmadan alarm politikasina baglanmaz.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import requests

import sentinel1_blind_target_bridge as bridge
import sentinel1_scene_probe as s1


PC_SEARCH_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
RTC_COLLECTION = "sentinel-1-rtc"
SEARCH_DAYS = 24
TIMEOUT_SECONDS = 35
MIN_MAIN_M2 = 250
MICRO_RANGE_M2 = [150, 249]


def _query_rtc_items(bbox, days=SEARCH_DAYS):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    payload = {
        "collections": [RTC_COLLECTION],
        "bbox": list(map(float, bbox)),
        "datetime": f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}",
        "limit": 80,
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    response = requests.post(PC_SEARCH_URL, json=payload, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json().get("features", [])


def _signed_href(item, pol):
    assets = item.get("assets") or {}
    asset = assets.get(str(pol).lower()) or assets.get(str(pol).upper())
    if not isinstance(asset, dict) or not asset.get("href"):
        return None
    href = str(asset["href"])
    try:
        import planetary_computer

        return planetary_computer.sign(href)
    except Exception:
        return href


def _window_radii(area_m2):
    area = float(area_m2 or 0)
    if area < 1000:
        return 1, 4  # 3x3 merkez, 9x9 cevre
    if area < 10000:
        return 2, 5  # 5x5 merkez, 11x11 cevre
    return 3, 7      # 7x7 merkez, 15x15 cevre


def _db(value):
    if value is None or not np.isfinite(value) or value <= 0:
        return None
    return float(10.0 * math.log10(float(value)))


def _summarize_patch(array, inner_radius):
    data = np.ma.array(array, copy=False)
    if data.ndim != 2 or data.shape[0] != data.shape[1] or data.shape[0] < 3:
        return None
    n = data.shape[0]
    c = n // 2
    y, x = np.ogrid[:n, :n]
    inner_mask = (np.abs(y - c) <= inner_radius) & (np.abs(x - c) <= inner_radius)
    outer_mask = ~inner_mask

    inner = np.ma.array(data, mask=np.ma.getmaskarray(data) | ~inner_mask).compressed()
    ring = np.ma.array(data, mask=np.ma.getmaskarray(data) | ~outer_mask).compressed()
    inner = inner[np.isfinite(inner) & (inner > 0)]
    ring = ring[np.isfinite(ring) & (ring > 0)]
    if inner.size < 3 or ring.size < 8:
        return None

    inner_med = float(np.median(inner))
    ring_med = float(np.median(ring))
    inner_db = _db(inner_med)
    ring_db = _db(ring_med)
    if inner_db is None or ring_db is None:
        return None
    return {
        "merkez_db": round(inner_db, 3),
        "cevre_db": round(ring_db, 3),
        "lokal_kontrast_db": round(inner_db - ring_db, 3),
        "merkez_gecerli_piksel": int(inner.size),
        "cevre_gecerli_piksel": int(ring.size),
    }


def _read_target_patch(item, pol, target):
    href = _signed_href(item, pol)
    if not href:
        return None, "ASSET_YOK"

    try:
        import rasterio
        from rasterio.windows import Window
        from rasterio.warp import transform
    except Exception:
        return None, "RASTERIO_YOK"

    inner_radius, outer_radius = _window_radii(target.get("alan_m2"))
    size = outer_radius * 2 + 1
    try:
        with rasterio.Env(
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
            GDAL_HTTP_MAX_RETRY="2",
            GDAL_HTTP_RETRY_DELAY="1",
        ):
            with rasterio.open(href) as src:
                if src.crs is None:
                    return None, "CRS_YOK"
                xs, ys = transform("EPSG:4326", src.crs, [float(target["boylam"])], [float(target["enlem"])])
                row, col = src.index(xs[0], ys[0])
                window = Window(col - outer_radius, row - outer_radius, size, size)
                arr = src.read(1, window=window, boundless=True, masked=True)
    except Exception as exc:
        return None, type(exc).__name__

    summary = _summarize_patch(arr, inner_radius)
    if summary is None:
        return None, "YETERSIZ_GECERLI_PIKSEL"
    summary["pencere_piksel"] = int(size)
    summary["merkez_yaricap_piksel"] = int(inner_radius)
    return summary, None


def _metric(old_summary, new_summary):
    if not old_summary or not new_summary:
        return None
    delta_inner = float(new_summary["merkez_db"]) - float(old_summary["merkez_db"])
    delta_contrast = float(new_summary["lokal_kontrast_db"]) - float(old_summary["lokal_kontrast_db"])
    return {
        "merkez_degisim_db": round(delta_inner, 3),
        "lokal_kontrast_degisim_db": round(delta_contrast, 3),
        "mutlak_lokal_kontrast_degisim_db": round(abs(delta_contrast), 3),
    }


def _polarization_evidence(metrics):
    values = {
        str(pol).upper(): float(row["mutlak_lokal_kontrast_degisim_db"])
        for pol, row in metrics.items()
        if isinstance(row, dict) and row.get("mutlak_lokal_kontrast_degisim_db") is not None
    }
    if not values:
        return {
            "durum": "VERI_YOK",
            "konservatif_skor_db": None,
            "ham_medyan_db": None,
            "tepe_db": None,
            "yayilim_db": None,
        }

    vals = list(values.values())
    vmax = max(vals)
    vmin = min(vals)
    vmed = float(np.median(vals))
    spread = vmax - vmin
    if len(vals) == 1:
        status = "TEK_POL_VERISI"
        conservative = vals[0]
    elif vmax >= 2.0 and vmin < 1.0:
        status = "TEK_POL_BASKIN"
        conservative = vmin
    elif vmin >= 2.0:
        status = "CIFT_POL_GUCLU"
        conservative = vmin
    elif vmin >= 1.0:
        status = "CIFT_POL_ORTA"
        conservative = vmin
    else:
        status = "CIFT_POL_ZAYIF"
        conservative = vmin

    return {
        "durum": status,
        "konservatif_skor_db": round(float(conservative), 3),
        "ham_medyan_db": round(vmed, 3),
        "tepe_db": round(vmax, 3),
        "yayilim_db": round(spread, 3),
    }


def _severity(metrics):
    vals = [
        float(row["mutlak_lokal_kontrast_degisim_db"])
        for row in metrics.values()
        if isinstance(row, dict) and row.get("mutlak_lokal_kontrast_degisim_db") is not None
    ]
    if not vals:
        return "VERI_YOK"
    vals.sort()
    vmax = max(vals)
    vmed = float(np.median(vals))
    evidence = _polarization_evidence(metrics)
    # Ham diagnostik esikler: saha kalibrasyonu olmadan alarma baglanmaz.
    if len(vals) >= 2 and min(vals) >= 2.0 and vmax >= 3.0:
        return "YUKSEK_LOKAL_DEGISIM_DIAGNOSTIK"
    if evidence["durum"] == "TEK_POL_BASKIN":
        return "TEK_POL_BASKIN_DIAGNOSTIK"
    if vmax >= 2.0 or vmed >= 1.5:
        return "ORTA_LOKAL_DEGISIM_DIAGNOSTIK"
    return "DUSUK_LOKAL_DEGISIM_DIAGNOSTIK"


def inspect_region(region_key, targets, search_fn=_query_rtc_items, read_fn=_read_target_patch):
    aoi = s1.AOIS[region_key]
    try:
        items = search_fn(aoi["bbox"])
    except requests.RequestException as exc:
        return {
            "bolge": region_key,
            "durum": "RTC_KAYNAK_GECICI_HATA",
            "hata": type(exc).__name__,
            "hedef_sayisi": len(targets),
            "alarm": False,
            "saha_gorevi": False,
        }

    pair = s1._find_same_geometry_pair(items, region_key)
    if not pair:
        return {
            "bolge": region_key,
            "durum": "KARSILASTIRILABILIR_RTC_CIFTI_YOK",
            "hedef_sayisi": len(targets),
            "alarm": False,
            "saha_gorevi": False,
        }

    older, newer = pair
    common_pols, _, _ = bridge._common_raster_polarizations(older, newer)
    common_pols = [pol for pol in common_pols if pol in ("VV", "VH")]
    checked = []

    for target in targets:
        row = dict(target)
        covered = bridge._target_covered_by_pair(target, older, newer)
        row.update({"rtc_cifti_kapsiyor": bool(covered), "alarm": False, "saha_gorevi": False})
        if not covered or not common_pols:
            row["sar_degisim_durumu"] = "KARSILASTIRMA_YOK"
            checked.append(row)
            continue

        pol_metrics = {}
        errors = {}
        raw = {}
        for pol in common_pols:
            old_summary, old_error = read_fn(older, pol, target)
            new_summary, new_error = read_fn(newer, pol, target)
            if old_error or new_error:
                errors[pol] = {"eski": old_error, "yeni": new_error}
                continue
            raw[pol] = {"eski": old_summary, "yeni": new_summary}
            metric = _metric(old_summary, new_summary)
            if metric:
                pol_metrics[pol] = metric

        row["polarizasyon_metrikleri"] = pol_metrics
        if raw:
            row["ham_ozet"] = raw
        if errors:
            row["okuma_hatalari"] = errors
        evidence = _polarization_evidence(pol_metrics)
        row["sar_degisim_durumu"] = _severity(pol_metrics)
        row["sar_polarizasyon_uyumu"] = evidence["durum"]
        if pol_metrics:
            row["sar_lokal_degisim_skor_db"] = evidence["konservatif_skor_db"]
            row["sar_lokal_degisim_ham_medyan_db"] = evidence["ham_medyan_db"]
            row["sar_lokal_degisim_tepe_db"] = evidence["tepe_db"]
            row["sar_polarizasyon_yayilim_db"] = evidence["yayilim_db"]
        checked.append(row)

    ranked = sorted(
        [r for r in checked if r.get("sar_lokal_degisim_skor_db") is not None],
        key=lambda r: r["sar_lokal_degisim_skor_db"],
        reverse=True,
    )
    return {
        "bolge": region_key,
        "durum": "RTC_LOKAL_DEGISIM_DIAGNOSTIGI_HAZIR" if ranked else "RTC_OKUNDU_AMA_METRIK_YOK",
        "eski_item": older.get("id"),
        "eski_tarih": s1._iso_datetime(older).date().isoformat() if s1._iso_datetime(older) else None,
        "yeni_item": newer.get("id"),
        "yeni_tarih": s1._iso_datetime(newer).date().isoformat() if s1._iso_datetime(newer) else None,
        "goreli_yorunge": s1._relative_orbit(newer),
        "orbit_yonu": s1._orbit_state(newer),
        "polarizasyonlar": common_pols,
        "hedef_sayisi": len(checked),
        "metrik_uretilen_hedef": len(ranked),
        "en_yuksek_diagnostikler": [
            {
                "enlem": r["enlem"],
                "boylam": r["boylam"],
                "alan_m2": r["alan_m2"],
                "kaynak": r["kaynak"],
                "sar_degisim_durumu": r["sar_degisim_durumu"],
                "sar_polarizasyon_uyumu": r.get("sar_polarizasyon_uyumu"),
                "sar_lokal_degisim_skor_db": r["sar_lokal_degisim_skor_db"],
                "sar_lokal_degisim_ham_medyan_db": r.get("sar_lokal_degisim_ham_medyan_db"),
                "sar_lokal_degisim_tepe_db": r.get("sar_lokal_degisim_tepe_db"),
            }
            for r in ranked[:5]
        ],
        "hedefler": checked,
        "alarm": False,
        "saha_gorevi": False,
    }


def _self_check():
    stable_old = np.ones((9, 9), dtype=float)
    stable_new = np.ones((9, 9), dtype=float) * 1.2
    a = _summarize_patch(stable_old, 1)
    b = _summarize_patch(stable_new, 1)
    metric = _metric(a, b)
    assert abs(metric["lokal_kontrast_degisim_db"]) < 0.01, metric

    changed = np.ones((9, 9), dtype=float)
    changed[3:6, 3:6] = 2.5
    c = _summarize_patch(changed, 1)
    metric2 = _metric(a, c)
    assert metric2["lokal_kontrast_degisim_db"] > 3.0, metric2
    assert _severity({"VV": metric2, "VH": metric2}) == "YUKSEK_LOKAL_DEGISIM_DIAGNOSTIK"

    single_pol = {
        "VV": {"mutlak_lokal_kontrast_degisim_db": 4.4},
        "VH": {"mutlak_lokal_kontrast_degisim_db": 0.123},
    }
    single_evidence = _polarization_evidence(single_pol)
    assert single_evidence["durum"] == "TEK_POL_BASKIN", single_evidence
    assert single_evidence["konservatif_skor_db"] == 0.123, single_evidence
    assert _severity(single_pol) == "TEK_POL_BASKIN_DIAGNOSTIK"

    targets = [{
        "enlem": 38.341406,
        "boylam": 26.643308,
        "alan_m2": 1200,
        "kaynak": "test",
        "neden": "BULUT",
        "mahalle_yaklasik": "Gulbahce",
    }]
    bbox = list(s1.AOIS["gulbahce"]["bbox"])

    def fake_item(day):
        return {
            "id": f"RTC_{day}",
            "bbox": bbox,
            "properties": {
                "datetime": f"2026-09-{day:02d}T16:14:00Z",
                "sat:relative_orbit": 29,
                "sat:orbit_state": "ascending",
                "sar:polarizations": ["VV", "VH"],
                "sar:instrument_mode": "IW",
            },
            "assets": {
                "vv": {"href": "https://example.test/vv.tif", "roles": ["data"]},
                "vh": {"href": "https://example.test/vh.tif", "roles": ["data"]},
            },
        }

    def fake_search(_bbox):
        return [fake_item(11), fake_item(5)]

    def fake_read(item, pol, target):
        if item["id"].endswith("05"):
            return {"merkez_db": 0.0, "cevre_db": 0.0, "lokal_kontrast_db": 0.0}, None
        return {"merkez_db": 3.5, "cevre_db": 0.0, "lokal_kontrast_db": 3.5}, None

    row = inspect_region("gulbahce", targets, search_fn=fake_search, read_fn=fake_read)
    assert row["durum"] == "RTC_LOKAL_DEGISIM_DIAGNOSTIGI_HAZIR", row
    assert row["metrik_uretilen_hedef"] == 1
    assert row["en_yuksek_diagnostikler"][0]["sar_polarizasyon_uyumu"] == "CIFT_POL_GUCLU"
    assert row["alarm"] is False and row["saha_gorevi"] is False
    print("Sentinel-1 RTC lokal degisim diagnostigi oz testi OK.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check-only", action="store_true")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    if args.self_check_only:
        _self_check()
        return

    targets = bridge._load_targets()
    rows = []
    for region_key in ("cesme", "uzunkuyu", "gulbahce"):
        try:
            rows.append(inspect_region(region_key, targets.get(region_key) or []))
        except Exception as exc:
            rows.append({
                "bolge": region_key,
                "durum": "DIAGNOSTIK_HATA",
                "hata": type(exc).__name__,
                "hedef_sayisi": len(targets.get(region_key) or []),
                "alarm": False,
                "saha_gorevi": False,
            })

    payload = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": MIN_MAIN_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "kalibrasyon_durumu": "HAM_SAR_DIAGNOSTIK_ESIKLERI_SAHA_DOGRULAMASI_BEKLIYOR",
        "yontem": "RTC merkez-vs-yakin-cevre lokal kontrast degisimi; iki polarizasyon varsa siralama skoru zayif polarizasyona gore konservatiftir, ham medyan/tepe ayrica raporlanir; tek-polarizasyon baskin sicramasi ayri diagnostik siniftir.",
        "bolgeler": rows,
        "toplam_hedef": sum(int(row.get("hedef_sayisi") or 0) for row in rows),
        "metrik_uretilen_hedef": sum(int(row.get("metrik_uretilen_hedef") or 0) for row in rows),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
