"""Sentinel-1 RTC diagnostiklerini harita icin deterministik GeoJSON'a donusturur.

Bu katman karar uretmez. 250 m2 ana esigi ve 150-249 m2 MIKRO bandini sadece
koruyucu metadata olarak tasir; alarm veya saha gorevi uretmez. Genis cevre,
tek-polarizasyon ve dusuk kanitli degisimleri silmez, arka plan katmaninda tutar.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


MAIN_MIN_M2 = 250
MICRO_RANGE_M2 = [150, 249]
LARGE_UNCONFIRMED_AREA_M2 = 10_000
BACKGROUND_LAYERS = {
    "SAR_GENIS_YUZEY_ARKA_PLAN",
    "SAR_DUSUK_KANIT_ARKA_PLAN",
    "SAR_VERI_YOK",
}
BROAD_SPATIAL = {
    "GENIS_CEVRE_HAREKETI_BASKIN",
    "GENIS_CEVRE_DEGISIMI_ESLIK_EDIYOR",
    "TEK_POL_CEVRE_DEGISIMI",
}
TEMPORAL_LOCAL_SUPPORT = {
    "ANI_YENI_LOKAL_BASLANGIC_DESTEKLI",
    "ARDISIK_GUCLU_LOKAL_HAREKET",
    "ARDISIK_ORTA_LOKAL_HAREKET",
}


def _load(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON okunamadi: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"JSON nesnesi bekleniyordu: {path}")
    return data


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coord_key(lat, lon):
    return round(float(lat), 6), round(float(lon), 6)


def _validate_policy(payload, label):
    if payload.get("alarm") is not False or payload.get("saha_gorevi") is not False:
        raise ValueError(f"{label}: diagnostik kaynak alarm/gorev uretmemeli")
    if int(_num(payload.get("ana_sentinel_esigi_m2")) or 0) != MAIN_MIN_M2:
        raise ValueError(f"{label}: ana Sentinel esigi {MAIN_MIN_M2} m2 olmali")
    interval = payload.get("mikro_aralik_m2") or []
    if list(interval) != MICRO_RANGE_M2:
        raise ValueError(f"{label}: MIKRO araligi {MICRO_RANGE_M2} olmali")


def _temporal_lookup(payload):
    result = {}
    for region in payload.get("bolgeler") or []:
        if not isinstance(region, dict):
            continue
        for row in region.get("hedefler") or []:
            if not isinstance(row, dict):
                continue
            lat = _num(row.get("enlem"))
            lon = _num(row.get("boylam"))
            if lat is None or lon is None:
                continue
            result[_coord_key(lat, lon)] = row
    return result


def _diagnostic_rows(payload, source_kind):
    for region in payload.get("bolgeler") or []:
        if not isinstance(region, dict):
            continue
        region_key = str(region.get("bolge") or "").strip().lower()
        old_date = region.get("eski_tarih") or region.get("bolgesel_referans_eski_tarih")
        new_date = region.get("yeni_tarih") or region.get("bolgesel_referans_yeni_tarih")
        for row in region.get("hedefler") or []:
            if not isinstance(row, dict):
                continue
            lat = _num(row.get("enlem"))
            lon = _num(row.get("boylam"))
            if lat is None or lon is None:
                continue
            item = dict(row)
            item["_bolge"] = region_key
            item["_eski_tarih"] = old_date
            item["_yeni_tarih"] = new_date
            item["_sar_kaynak_tipi"] = source_kind
            yield item


def _merge_rtc_gapfill(rtc, gapfill):
    merged = {}
    for row in _diagnostic_rows(rtc, "BOLGESEL_RTC"):
        merged[_coord_key(row["enlem"], row["boylam"])] = row
    for row in _diagnostic_rows(gapfill, "HEDEFE_OZEL_GAPFILL"):
        key = _coord_key(row["enlem"], row["boylam"])
        current = merged.get(key)
        current_score = _num((current or {}).get("sar_lokal_degisim_skor_db"))
        gap_score = _num(row.get("sar_lokal_degisim_skor_db"))
        if current is None or (current_score is None and gap_score is not None):
            merged[key] = row
    return merged


def _layer(row, temporal):
    target_layer = str(row.get("hedef_katmani") or "").strip().upper()
    spatial = str(row.get("sar_mekansal_ayrim") or "").strip().upper()
    score = _num(row.get("sar_lokal_degisim_skor_db"))
    area = _num(row.get("alan_m2"))
    temporal_status = str((temporal or {}).get("temporal_durum") or "").strip().upper()

    if target_layer == "MIKRO_DIAGNOSTIK":
        # Haritada aktif MIKRO adayi yalniz mevcut birlesik MIKRO karar kapisini
        # gecmis hedef olsun. Sahada zaten dogrulanmis kalibrasyon noktasi SAR'da
        # zayifsa veri kaybolmaz; dusuk-kanit arka planinda izlenir.
        if bool(row.get("mikro_guclu_diagnostik")):
            return "SAR_MIKRO_DIAGNOSTIK"
        return "SAR_DUSUK_KANIT_ARKA_PLAN"
    if target_layer == "SAHA_ONCUL_SAR_DIAGNOSTIK":
        # Temporal guard sahada dogrulanmis yikim/temizlik onculunde zayif tabandan
        # yeni, cift-polarizasyonlu ve lokal bir yukselisi ayri sinifa koyuyorsa,
        # 2 dB guclu-esigini bekletmeden haritada "gucleniyor" olarak gorunur kıl.
        # Bu yalniz diagnostik sunumdur; alarm/gorev uretmez ve 250 m2 esigini degistirmez.
        if temporal_status == "SAHA_ONCUL_TAZE_LOKAL_YUKSELIS":
            return "SAR_SAHA_ONCUL_GUCLENIYOR"
        if score is not None and score >= 2.0 and spatial == "KOMPAKT_LOKAL_DESTEKLI":
            return "SAR_SAHA_ONCUL_GUCLENIYOR"
        return "SAR_SAHA_ONCUL"
    if score is None:
        return "SAR_VERI_YOK"
    if score >= 2.0 and spatial == "KOMPAKT_LOKAL_DESTEKLI":
        if temporal_status == "ANI_YENI_LOKAL_BASLANGIC_GENIS_ARKA_PLANLI":
            return "SAR_GUCLU_LOKAL_GENIS_ARKA_PLANLI"
        return "SAR_GUCLU_LOKAL"
    if spatial in BROAD_SPATIAL:
        return "SAR_GENIS_YUZEY_ARKA_PLAN"
    if score >= 1.0 and spatial == "LOKAL_AYRIM_DESTEKLI":
        # 10.000 m2+ orta-kuvvette tek aralik lokal sinyal, operasyon tarafinda da
        # taze-kazi onceligi alamaz. Temporal ani baslangic/devam kaniti yoksa haritada
        # aktif aday gibi gostermek yerine veriyi koruyup arka planda izle.
        if (
            area is not None
            and area >= LARGE_UNCONFIRMED_AREA_M2
            and temporal_status not in TEMPORAL_LOCAL_SUPPORT
        ):
            return "SAR_DUSUK_KANIT_ARKA_PLAN"
        return "SAR_LOKAL_ORTA"
    return "SAR_DUSUK_KANIT_ARKA_PLAN"


def build_geojson(rtc, temporal, gapfill):
    _validate_policy(rtc, "RTC")
    _validate_policy(temporal, "TEMPORAL")
    _validate_policy(gapfill, "GAPFILL")

    temporal_by_coord = _temporal_lookup(temporal)
    merged = _merge_rtc_gapfill(rtc, gapfill)
    features = []
    pair_dates = {}

    for region in rtc.get("bolgeler") or []:
        if not isinstance(region, dict):
            continue
        region_key = str(region.get("bolge") or "").strip().lower()
        pair_dates[region_key] = {
            "eski_tarih": region.get("eski_tarih"),
            "yeni_tarih": region.get("yeni_tarih"),
        }

    for key, row in sorted(
        merged.items(),
        key=lambda item: (str(item[1].get("_bolge") or ""), item[0][0], item[0][1]),
    ):
        lat, lon = key
        temporal_row = temporal_by_coord.get(key) or {}
        layer = _layer(row, temporal_row)
        area = _num(row.get("alan_m2"))
        score = _num(row.get("sar_lokal_degisim_skor_db"))
        properties = {
            "bolge": row.get("_bolge") or "",
            "alan_m2": int(round(area)) if area is not None else None,
            "kaynak": row.get("kaynak"),
            "sar_kaynak_tipi": row.get("_sar_kaynak_tipi"),
            "hedef_katmani": row.get("hedef_katmani"),
            "neden": row.get("neden"),
            "mahalle_yaklasik": row.get("mahalle_yaklasik"),
            "harita_katmani": layer,
            "arka_plan": layer in BACKGROUND_LAYERS,
            "sar_lokal_degisim_skor_db": round(score, 3) if score is not None else None,
            "sar_polarizasyon_uyumu": row.get("sar_polarizasyon_uyumu"),
            "sar_mekansal_ayrim": row.get("sar_mekansal_ayrim"),
            "sar_cevre_degisim_konservatif_db": row.get("sar_cevre_degisim_konservatif_db"),
            "sar_cevre_degisim_tepe_db": row.get("sar_cevre_degisim_tepe_db"),
            "eski_tarih": row.get("_eski_tarih"),
            "yeni_tarih": row.get("_yeni_tarih"),
            "temporal_durum": temporal_row.get("temporal_durum"),
            "onceki_sar_lokal_degisim_skor_db": temporal_row.get("onceki_sar_lokal_degisim_skor_db"),
            "saha_dogrulanmis_yikim_onculu": bool(row.get("saha_dogrulanmis_yikim_onculu")),
            "saha_oncul_gorev_id": row.get("saha_oncul_gorev_id"),
            "saha_oncul_son_tarih": row.get("saha_oncul_son_tarih"),
            "mikro_saha_dogrulandi": bool(row.get("mikro_saha_dogrulandi")),
            "mikro_guclu_diagnostik": bool(row.get("mikro_guclu_diagnostik")),
            "alarm": False,
            "saha_gorevi": False,
        }
        features.append(
            {
                "type": "Feature",
                "id": f"{properties['bolge']}:{lat:.6f}:{lon:.6f}",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": properties,
            }
        )

    layer_counts = {}
    region_counts = {}
    for feature in features:
        props = feature["properties"]
        layer_counts[props["harita_katmani"]] = layer_counts.get(props["harita_katmani"], 0) + 1
        region_counts[props["bolge"]] = region_counts.get(props["bolge"], 0) + 1

    return {
        "type": "FeatureCollection",
        "name": "sentinel1_rtc_ground_diagnostics",
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": MAIN_MIN_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "rtc_pair_dates": pair_dates,
        "feature_count": len(features),
        "layer_counts": dict(sorted(layer_counts.items())),
        "region_counts": dict(sorted(region_counts.items())),
        "not": (
            "Harita katmani diagnostiktir; Sentinel-1 geri sacilim degisimi tek basina "
            "insaat/kazi kaniti, alarm, saha gorevi, adres veya parsel dogrulamasi degildir."
        ),
        "features": features,
    }


def _self_check():
    base = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
    }
    rtc = {
        **base,
        "bolgeler": [
            {
                "bolge": "gulbahce",
                "eski_tarih": "2026-09-06",
                "yeni_tarih": "2026-09-12",
                "hedefler": [
                    {
                        "enlem": 38.34231,
                        "boylam": 26.642621,
                        "alan_m2": 400,
                        "hedef_katmani": "ANA_250_PLUS",
                        "sar_lokal_degisim_skor_db": 3.394,
                        "sar_mekansal_ayrim": "KOMPAKT_LOKAL_DESTEKLI",
                    },
                    {
                        "enlem": 38.33,
                        "boylam": 26.64,
                        "alan_m2": 400,
                        "hedef_katmani": "SAHA_ONCUL_SAR_DIAGNOSTIK",
                        "sar_lokal_degisim_skor_db": 1.1,
                        "sar_mekansal_ayrim": "KARISIK_DUSUK_LOKALLIK",
                        "saha_dogrulanmis_yikim_onculu": True,
                    },
                    {
                        "enlem": 38.31,
                        "boylam": 26.63,
                        "alan_m2": 500,
                        "hedef_katmani": "ANA_250_PLUS",
                        "sar_lokal_degisim_skor_db": 0.4,
                        "sar_mekansal_ayrim": "GENIS_CEVRE_HAREKETI_BASKIN",
                    },
                ],
            },
            {
                "bolge": "cesme",
                "eski_tarih": "2026-09-06",
                "yeni_tarih": "2026-09-12",
                "hedefler": [
                    {
                        "enlem": 38.287679,
                        "boylam": 26.266305,
                        "alan_m2": 200,
                        "hedef_katmani": "MIKRO_DIAGNOSTIK",
                        "sar_lokal_degisim_skor_db": 0.438,
                        "sar_mekansal_ayrim": "TEK_POL_CEVRE_DEGISIMI",
                        "mikro_saha_dogrulandi": True,
                        "mikro_guclu_diagnostik": False,
                    }
                ],
            },
        ],
    }
    temporal = {
        **base,
        "bolgeler": [
            {
                "bolge": "gulbahce",
                "hedefler": [
                    {
                        "enlem": 38.34231,
                        "boylam": 26.642621,
                        "temporal_durum": "ANI_YENI_LOKAL_BASLANGIC_GENIS_ARKA_PLANLI",
                        "onceki_sar_lokal_degisim_skor_db": 0.038,
                    }
                ],
            }
        ],
    }
    gapfill = {**base, "bolgeler": []}
    geo = build_geojson(rtc, temporal, gapfill)
    assert geo["feature_count"] == 4
    layers = {f["properties"]["harita_katmani"] for f in geo["features"]}
    assert "SAR_GUCLU_LOKAL_GENIS_ARKA_PLANLI" in layers
    assert "SAR_SAHA_ONCUL" in layers
    assert "SAR_DUSUK_KANIT_ARKA_PLAN" in layers
    assert "SAR_GENIS_YUZEY_ARKA_PLAN" in layers
    assert _layer(
        {
            "hedef_katmani": "MIKRO_DIAGNOSTIK",
            "mikro_guclu_diagnostik": True,
        },
        {},
    ) == "SAR_MIKRO_DIAGNOSTIK"
    assert _layer(
        {
            "hedef_katmani": "MIKRO_DIAGNOSTIK",
            "mikro_saha_dogrulandi": True,
            "mikro_guclu_diagnostik": False,
        },
        {},
    ) == "SAR_DUSUK_KANIT_ARKA_PLAN"
    assert _layer(
        {
            "hedef_katmani": "SAHA_ONCUL_SAR_DIAGNOSTIK",
            "sar_lokal_degisim_skor_db": 1.8,
            "sar_mekansal_ayrim": "LOKAL_AYRIM_DESTEKLI",
        },
        {"temporal_durum": "SAHA_ONCUL_TAZE_LOKAL_YUKSELIS"},
    ) == "SAR_SAHA_ONCUL_GUCLENIYOR"
    assert _layer(
        {
            "hedef_katmani": "ANA_250_PLUS",
            "alan_m2": 12_000,
            "sar_lokal_degisim_skor_db": 1.5,
            "sar_mekansal_ayrim": "LOKAL_AYRIM_DESTEKLI",
        },
        {},
    ) == "SAR_DUSUK_KANIT_ARKA_PLAN"
    assert _layer(
        {
            "hedef_katmani": "ANA_250_PLUS",
            "alan_m2": 800,
            "sar_lokal_degisim_skor_db": 1.5,
            "sar_mekansal_ayrim": "LOKAL_AYRIM_DESTEKLI",
        },
        {},
    ) == "SAR_LOKAL_ORTA"
    assert _layer(
        {
            "hedef_katmani": "ANA_250_PLUS",
            "alan_m2": 12_000,
            "sar_lokal_degisim_skor_db": 1.5,
            "sar_mekansal_ayrim": "LOKAL_AYRIM_DESTEKLI",
        },
        {"temporal_durum": "ARDISIK_ORTA_LOKAL_HAREKET"},
    ) == "SAR_LOKAL_ORTA"
    assert geo["alarm"] is False and geo["saha_gorevi"] is False
    assert geo["ana_sentinel_esigi_m2"] == 250
    assert geo["mikro_aralik_m2"] == [150, 249]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rtc")
    parser.add_argument("--temporal")
    parser.add_argument("--gapfill")
    parser.add_argument("--output", default="sentinel1_rtc_map.geojson")
    parser.add_argument("--self-check-only", action="store_true")
    args = parser.parse_args()

    if args.self_check_only:
        _self_check()
        print("sentinel1_rtc_map_export self-check OK")
        return
    if not args.rtc or not args.temporal or not args.gapfill:
        parser.error("--rtc, --temporal ve --gapfill gerekli")

    payload = build_geojson(_load(args.rtc), _load(args.temporal), _load(args.gapfill))
    Path(args.output).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": args.output,
        "feature_count": payload["feature_count"],
        "layer_counts": payload["layer_counts"],
        "alarm": False,
        "saha_gorevi": False,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
