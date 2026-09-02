"""Gülbahçe doğu kör şeridi için ayrı, alarm-dışı Sentinel kapsama taraması.

Mevcut ``uzunkuyu`` üretim kutusunun doğu sınırı 26.66 E'dir. Gülbahçe
operasyon tamponunun küçük bir bölümü bu sınırın doğusunda kaldığı için bu
dosya yalnız o şeridi ayrı bir Sentinel bölgesi olarak tarar. Ana 250 m² üretim
eşiğini değiştirmez ve mevcut saha görevlerine dokunmaz.

Ayrıca 26.67 E'ye uzatılmış tam Uzunkuyu kutusunun güncel Earth Search
ürünlerinde tek Sentinel karesiyle güvenli biçimde karşılaştırılabilir olup
olmadığını yalnız metadata seviyesinde sınar. Bu probe başarılı kalırsa ileride
çekirdek bbox küçük bir değişiklikle genişletilebilir; başarısızsa ayrı şerit
taraması kör alanı kapatmaya devam eder.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import satellite
from micro_site_audit import _micro_candidates


ISTANBUL = ZoneInfo("Europe/Istanbul")
OUTPUT_FILE = Path(__file__).with_name("gulbahce_east_strip_scan.json")

REGION_KEY = "gulbahce_east_strip"
REGION_LABEL = "Gülbahçe doğu kör-şerit diagnostik taraması"
# Eski Uzunkuyu sınırının hemen doğusunu kapsar. İdari/kadastral sınır değildir.
STRIP_BBOX = [26.6600, 38.3000, 26.6720, 38.3530]
OLD_UZUNKUYU_EAST = 26.6600
PROPOSED_UZUNKUYU_BBOX = [26.45, 38.18, 26.67, 38.43]

# Repoda iki farklı amaçla kullanılan Gülbahçe referans noktası var. Hangisinin
# "mahalle merkezi" olduğunu varsaymıyoruz; her ikisini de operasyon referansı
# kabul edip doğu kör şeridinin bunların 2 km tamponunu örttüğünü ölçüyoruz.
REFERENCE_POINTS = {
    "coverage_guard": (38.33278, 26.64556),
    "micro_audit": (38.319473, 26.646463),
}
TARGET_RADIUS_M = 2_000


def _meters_per_degree_lon(latitude: float) -> float:
    import math

    return 111_320.0 * math.cos(math.radians(latitude))


def _east_edge_distance_m(latitude: float, longitude: float, east: float) -> float:
    return (float(east) - float(longitude)) * _meters_per_degree_lon(latitude)


def _north_south_margin_m(latitude: float, south: float, north: float) -> tuple[float, float]:
    return (
        (float(latitude) - float(south)) * 110_570.0,
        (float(north) - float(latitude)) * 110_570.0,
    )


def _install_region() -> None:
    satellite.REGIONS[REGION_KEY] = {
        "label": REGION_LABEL,
        "bbox": list(STRIP_BBOX),
    }


def _pixel_metrics(bbox):
    height, width = satellite._output_shape(bbox)
    west, south, east, north = map(float, bbox)
    mean_lat = (south + north) / 2.0
    pixel_width_m = (
        (east - west)
        * 111_320.0
        * __import__("math").cos(__import__("math").radians(mean_lat))
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


def _pair_metadata(older, latest):
    return {
        "onceki_tarih": satellite._item_date(older),
        "son_tarih": satellite._item_date(latest),
        "onceki_item": older.get("id"),
        "son_item": latest.get("id"),
        "onceki_orbit": satellite._relative_orbit(older),
        "son_orbit": satellite._relative_orbit(latest),
    }


def _probe_extended_bbox():
    try:
        items = satellite._search_items(PROPOSED_UZUNKUYU_BBOX)
        older, latest = satellite._pick_pair(items, bbox=PROPOSED_UZUNKUYU_BBOX)
        return {
            "durum": "uygun",
            "tam_kutu_genisletme_guvenli_adayi": True,
            **_pair_metadata(older, latest),
        }
    except Exception as exc:
        return {
            "durum": "uygun_degil",
            "tam_kutu_genisletme_guvenli_adayi": False,
            "hata": f"{type(exc).__name__}: {exc}",
        }


def _annotate_new_strip(rows):
    annotated = []
    for row in rows:
        item = dict(row)
        longitude = float(item.get("boylam", 0.0))
        item["eski_uzunkuyu_kapsami_disinda"] = longitude > OLD_UZUNKUYU_EAST
        annotated.append(item)
    return annotated


def _build_core():
    _install_region()
    older, latest = satellite.sentinel_pair(REGION_KEY)
    main = satellite.analyze_sentinel_change(REGION_KEY, pair=(older, latest))
    micro = _micro_candidates(REGION_KEY, pair=(older, latest))

    main_rows = _annotate_new_strip(main.get("hotspots", []))
    micro_rows = _annotate_new_strip(micro.get("adaylar", []))
    new_main = [row for row in main_rows if row["eski_uzunkuyu_kapsami_disinda"]]
    new_micro = [row for row in micro_rows if row["eski_uzunkuyu_kapsami_disinda"]]

    refs = {}
    for name, (latitude, longitude) in REFERENCE_POINTS.items():
        south_margin, north_margin = _north_south_margin_m(
            latitude, STRIP_BBOX[1], STRIP_BBOX[3]
        )
        refs[name] = {
            "enlem": latitude,
            "boylam": longitude,
            "eski_dogu_sinira_mesafe_m": round(
                (OLD_UZUNKUYU_EAST - longitude)
                * _meters_per_degree_lon(latitude)
            ),
            "yeni_serit_dogu_kenara_mesafe_m": round(
                _east_edge_distance_m(latitude, longitude, STRIP_BBOX[2])
            ),
            "serit_guney_marj_m": round(south_margin),
            "serit_kuzey_marj_m": round(north_margin),
        }

    return {
        "amac": (
            "Mevcut Uzunkuyu 26.66 E doğu sınırının dışında kalan Gülbahçe "
            "operasyon şeridini ayrı Sentinel taramasıyla kör bırakmamak. "
            "Alarm veya otomatik saha görevi üretmez."
        ),
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": satellite.MIN_HOTSPOT_AREA_M2,
        "mikro_aralik_m2": [150, 249],
        "eski_uzunkuyu_dogu_siniri": OLD_UZUNKUYU_EAST,
        "serit_bbox": list(STRIP_BBOX),
        "analiz_grid": _pixel_metrics(STRIP_BBOX),
        "referanslar": refs,
        "sentinel_cifti": _pair_metadata(older, latest),
        "ana_250_plus_aday_sayisi": len(main_rows),
        "ana_250_plus_adaylar": main_rows,
        "yeni_dogu_serit_ana_aday_sayisi": len(new_main),
        "yeni_dogu_serit_ana_adaylar": new_main,
        "ham_mikro_150_249_aday_sayisi": len(micro_rows),
        "ham_mikro_150_249_adaylar": micro_rows,
        "yeni_dogu_serit_ham_mikro_aday_sayisi": len(new_micro),
        "yeni_dogu_serit_ham_mikro_adaylar": new_micro,
        "tam_uzunkuyu_bbox_genisletme_probe": _probe_extended_bbox(),
        "not": (
            "Mikro satırlar ham diagnostiktir; temporal/lokalite kanıt birleştirme "
            "kapısını geçmeden saha adayına yükseltilmez. Koordinatlar ada/parsel "
            "veya hukuki statü doğrulaması değildir."
        ),
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
        print("Gülbahçe doğu şerit sonucu değişmedi; JSON yeniden yazılmadı.")
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
    _install_region()
    assert satellite.MIN_HOTSPOT_AREA_M2 == 250
    assert STRIP_BBOX[0] == OLD_UZUNKUYU_EAST
    assert STRIP_BBOX[2] >= 26.67
    pixel = _pixel_metrics(STRIP_BBOX)
    assert pixel["pixel_edge_max_m"] <= 10.5, pixel

    for latitude, longitude in REFERENCE_POINTS.values():
        assert longitude < OLD_UZUNKUYU_EAST
        assert _east_edge_distance_m(latitude, longitude, STRIP_BBOX[2]) >= TARGET_RADIUS_M
        south_margin, north_margin = _north_south_margin_m(
            latitude, STRIP_BBOX[1], STRIP_BBOX[3]
        )
        assert south_margin >= TARGET_RADIUS_M
        assert north_margin >= TARGET_RADIUS_M

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
    probe = core["tam_uzunkuyu_bbox_genisletme_probe"]
    print(
        "Gülbahçe doğu şerit taraması tamamlandı: "
        f"250+={core['yeni_dogu_serit_ana_aday_sayisi']}, "
        f"ham mikro={core['yeni_dogu_serit_ham_mikro_aday_sayisi']}, "
        f"tam bbox probe={probe['durum']}."
    )


if __name__ == "__main__":
    main()
