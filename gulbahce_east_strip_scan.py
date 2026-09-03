"""Gülbahçe doğu kapsaması için alarm-dışı regresyon koruması.

Gülbahçe'nin eski 26.66 E doğu kör şeridi artık ``uzunkuyu`` ana Sentinel
üretim kutusuna 26.67 E'ye kadar entegredir. Bu yardımcı katman bu nedenle aynı
Sentinel görüntülerini ikinci kez indirip analiz etmez. Bunun yerine ana üretim
bbox'unun Gülbahçe referanslarının 2 km operasyon tamponunu kapsadığını ve 10 m
sınıfı analiz ölçeğinin korunduğunu ölçer.

Katman alarm veya saha görevi üretmez; ana 250 m² eşiğini ve 150-249 m² MİKRO
ŞANTİYE karar zincirini değiştirmez. Amaç, eski kör alanın ileride yanlışlıkla
geri gelmesini görünür bir kalite hatasına dönüştürmek ve gereksiz mükerrer
Sentinel taramasını ortadan kaldırmaktır.
"""

from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import satellite


ISTANBUL = ZoneInfo("Europe/Istanbul")
OUTPUT_FILE = Path(__file__).with_name("gulbahce_east_strip_scan.json")
REGION_KEY = "uzunkuyu"
REGION_LABEL = "Gülbahçe doğu kapsama regresyon koruması"

OLD_UZUNKUYU_EAST = 26.6600
REQUIRED_UZUNKUYU_EAST = 26.6700
TARGET_RADIUS_M = 2_000

# Repoda farklı diagnostik amaçlarla kullanılan iki operasyon referansı korunur.
# Bunlar idari/kadastral mahalle merkezi veya ada/parsel değildir.
REFERENCE_POINTS = {
    "coverage_guard": (38.33278, 26.64556),
    "micro_audit": (38.319473, 26.646463),
}


def _meters_per_degree_lon(latitude: float) -> float:
    return 111_320.0 * math.cos(math.radians(latitude))


def _edge_margins_m(bbox, latitude: float, longitude: float) -> dict:
    west, south, east, north = map(float, bbox)
    lon_scale = _meters_per_degree_lon(latitude)
    return {
        "bati": round((longitude - west) * lon_scale),
        "dogu": round((east - longitude) * lon_scale),
        "guney": round((latitude - south) * 110_570.0),
        "kuzey": round((north - latitude) * 110_570.0),
    }


def _pixel_metrics(bbox):
    height, width = satellite._output_shape(bbox)
    west, south, east, north = map(float, bbox)
    mean_lat = (south + north) / 2.0
    pixel_width_m = (
        (east - west)
        * 111_320.0
        * math.cos(math.radians(mean_lat))
        / width
    )
    pixel_height_m = (north - south) * 110_570.0 / height
    return {
        "height": int(height),
        "width": int(width),
        "pixel_width_m": round(pixel_width_m, 2),
        "pixel_height_m": round(pixel_height_m, 2),
        "pixel_edge_max_m": round(max(pixel_width_m, pixel_height_m), 2),
    }


def _build_core():
    region = satellite.REGIONS[REGION_KEY]
    bbox = list(map(float, region["bbox"]))
    pixel = _pixel_metrics(bbox)

    refs = {}
    issues = []
    for name, (latitude, longitude) in REFERENCE_POINTS.items():
        margins = _edge_margins_m(bbox, latitude, longitude)
        min_margin = min(margins.values())
        fully_covered = min_margin >= TARGET_RADIUS_M
        refs[name] = {
            "enlem": latitude,
            "boylam": longitude,
            "kenar_mesafeleri_m": margins,
            "en_yakin_kenar_m": min_margin,
            "2km_operasyon_tamponu_kapsaniyor": fully_covered,
            "not": "Operasyonel referanstır; idari/kadastral sınır veya ada-parsel değildir.",
        }
        if not fully_covered:
            issues.append(f"{name.upper()}_2KM_TAMPONU_TAM_KAPSANMIYOR")

    if bbox[2] < REQUIRED_UZUNKUYU_EAST:
        issues.append("UZUNKUYU_DOGU_SINIRI_GULBAHCE_ICIN_GERI_CEKILMIS")
    if pixel["pixel_edge_max_m"] > 10.5:
        issues.append("ANALIZ_COZUNURLUGU_10M_SINIFINDAN_UZAKLASIYOR")
    if satellite.MIN_HOTSPOT_AREA_M2 != 250:
        issues.append("ANA_SENTINEL_ESIGI_250M2_DEGIL")

    integrated = not issues
    return {
        "amac": (
            "Gülbahçe'nin daha önce 26.66 E doğusunda kalan kör şeridinin ana "
            "Uzunkuyu Sentinel üretim kutusuna entegre kalmasını ölçmek. Aynı "
            "görüntüleri ikinci kez analiz etmez; alarm veya saha görevi üretmez."
        ),
        "durum": "ok" if integrated else "dikkat_gerekiyor",
        "sorunlar": issues,
        "alarm": False,
        "saha_gorevi": False,
        "ayri_sentinel_tarama_yapildi": False,
        "mukerrer_goruntu_analizi_engellendi": True,
        "ana_uretim_esigi_m2": satellite.MIN_HOTSPOT_AREA_M2,
        "mikro_aralik_m2": [150, 249],
        "ana_uretim_bolgesi": REGION_KEY,
        "ana_uretim_etiketi": region["label"],
        "ana_uretim_bbox": bbox,
        "eski_uzunkuyu_dogu_siniri": OLD_UZUNKUYU_EAST,
        "gereken_dogu_sinir": REQUIRED_UZUNKUYU_EAST,
        "dogu_kapsama_entegre": bbox[2] >= REQUIRED_UZUNKUYU_EAST,
        "analiz_grid": pixel,
        "referanslar": refs,
        "eski_ayri_serit_modu": "devre_disi",
        "not": (
            "250 m²+ aday üretimi ana günlük Sentinel taramasındadır. 150-249 m² "
            "MİKRO ŞANTİYE adayları da ana mikro karar zincirinde temporal/lokalite "
            "kanıtıyla değerlendirilir. Bu dosya yalnız kapsama regresyonunu izler."
        ),
        # Eski JSON tüketicileri için alan adını koruyoruz; artık metadata probe
        # değil, ana üretime entegrasyon durumunu bildiriyor.
        "tam_uzunkuyu_bbox_genisletme_probe": {
            "durum": "ana_kapsama_entegre" if bbox[2] >= REQUIRED_UZUNKUYU_EAST else "regresyon",
            "tam_kutu_genisletme_guvenli_adayi": bbox[2] >= REQUIRED_UZUNKUYU_EAST,
            "harici_sentinel_sorgusu_yapildi": False,
        },
    }


def _write_if_changed(core):
    previous_core = None
    if OUTPUT_FILE.exists():
        try:
            previous = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
            if isinstance(previous, dict):
                previous_core = {
                    key: value for key, value in previous.items() if key != "olusturma"
                }
        except (OSError, ValueError, json.JSONDecodeError):
            previous_core = None

    if previous_core == core:
        print("Gülbahçe doğu kapsama regresyon sonucu değişmedi; JSON yeniden yazılmadı.")
        return False

    payload = {
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        **core,
    }
    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def _self_check():
    assert satellite.MIN_HOTSPOT_AREA_M2 == 250
    bbox = satellite.REGIONS[REGION_KEY]["bbox"]
    assert bbox[2] >= REQUIRED_UZUNKUYU_EAST, bbox

    pixel = _pixel_metrics(bbox)
    assert pixel["pixel_edge_max_m"] <= 10.5, pixel

    for latitude, longitude in REFERENCE_POINTS.values():
        margins = _edge_margins_m(bbox, latitude, longitude)
        assert min(margins.values()) >= TARGET_RADIUS_M, margins

    print("gulbahce_east_strip_scan self-check: OK")


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()

    if args.self_check:
        _self_check()
        return

    _self_check()
    core = _build_core()
    _write_if_changed(core)
    print(
        "Gülbahçe doğu kapsama regresyon kontrolü tamamlandı: "
        f"durum={core['durum']}, ayrı Sentinel taraması={core['ayri_sentinel_tarama_yapildi']}."
    )


if __name__ == "__main__":
    main()
