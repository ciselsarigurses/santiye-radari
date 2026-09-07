"""Diyagonal birleşmiş Sentinel adaylarının 4-komşu alt-küme koordinatlarını ölçer.

Bu modül üretim alarmını, 250 m² ana eşiğini, aday sayısını veya saha görevlerini
DEĞİŞTİRMEZ. Ana Sentinel analizinin zaten geçerli bulduğu değişim maskesini yalnız
geometrik olarak 4-komşulukla yeniden okur. 8-komşulukta sırf köşeden temas nedeniyle
tek aday olan bir küme, 4-komşulukta iki veya daha fazla geçerli parçaya ayrılıyorsa
her parçanın değişmiş bir piksel üzerindeki temsil koordinatını diagnostik olarak
kaydeder. Amaç birleşmiş ebeveyn merkezinin iki ayrı kazı/temel başlangıcını tek bir
koordinat gibi göstermesi riskini görünür kılmaktır.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import satellite
from candidate_capacity_audit import (
    _ORIGINAL_HOTSPOTS,
    _analyze_with_hotspot_function,
    _component_point,
    _eligible_component,
    _four_connected_components,
    _stored_snapshot,
)
from daily_report import ISTANBUL, REPORT_REGIONS, ensure_daily_schema
from scanner import connect


OUTPUT_FILE = Path(__file__).with_name("diagonal_child_coordinate_audit.json")
CAPACITY_FILE = Path(__file__).with_name("candidate_capacity_audit.json")
MAX_CHILD_AREA_M2 = 10_000


def _latest_stored_report_date():
    """Duvar saati yerine en son üretilmiş uydu rapor gününü kullanır.

    Gece yarısından sonra 11:00 ana taramasından önce çalıştırıldığında bugünün satırı
    henüz yok olabilir. Bu durumda diagnostik boşalmasın diye mevcut kapasite raporunun
    veri gününü, o da yoksa DB'deki son uydu rapor gününü kullanır.
    """
    try:
        payload = json.loads(CAPACITY_FILE.read_text(encoding="utf-8"))
        value = str(payload.get("rapor_tarihi") or "").strip()
        if value:
            return value
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    with connect() as connection:
        row = connection.execute(
            "SELECT MAX(rapor_tarihi) FROM gunluk_uydu_raporlari"
        ).fetchone()
    return str(row[0]).strip() if row and row[0] else None


def _child_geometry(change_mask, bbox, pixel_area_m2, small_site_mask=None):
    """Geçerli 8-komşu ebeveynleri ve yalnız köşeden ayrılan 4-komşu çocukları döndürür."""
    eight_components = satellite._connected_components(change_mask)
    four_components = _four_connected_components(change_mask)

    parent_by_pixel = {}
    eligible_eight = set()
    for parent_index, component in enumerate(eight_components):
        for pixel in component:
            parent_by_pixel[pixel] = parent_index
        if _eligible_component(component, pixel_area_m2, small_site_mask):
            eligible_eight.add(parent_index)

    children_by_parent = {}
    for component in four_components:
        if not _eligible_component(component, pixel_area_m2, small_site_mask):
            continue
        parent_index = parent_by_pixel.get(component[0])
        if parent_index is None or parent_index not in eligible_eight:
            continue
        children_by_parent.setdefault(parent_index, []).append(component)

    parents = []
    for parent_index, children in children_by_parent.items():
        if len(children) < 2:
            continue

        parent = eight_components[parent_index]
        parent_lat, parent_lon = _component_point(parent, bbox, change_mask.shape)
        child_rows = []
        for component in children:
            area_m2 = round(len(component) * pixel_area_m2)
            if not (satellite.MIN_HOTSPOT_AREA_M2 <= area_m2 <= MAX_CHILD_AREA_M2):
                continue
            lat, lon = _component_point(component, bbox, change_mask.shape)
            child_rows.append(
                {
                    "enlem": lat,
                    "boylam": lon,
                    "alan_m2": area_m2,
                    "piksel_sayisi": len(component),
                    "koordinat_tipi": "4-komsu_degisim_pikseli_temsil_noktasi",
                }
            )

        if len(child_rows) < 2:
            continue
        child_rows.sort(key=lambda item: (-item["alan_m2"], item["enlem"], item["boylam"]))
        parents.append(
            {
                "ebeveyn_enlem": parent_lat,
                "ebeveyn_boylam": parent_lon,
                "sekiz_komsu_alan_m2": round(len(parent) * pixel_area_m2),
                "dort_komsu_alt_aday_sayisi": len(child_rows),
                "alt_adaylar": child_rows,
                "geometri_nedeni": "yalniz_kose_temasi_8_komsuda_birlestiriyor",
            }
        )

    parents.sort(
        key=lambda item: (
            -item["sekiz_komsu_alan_m2"],
            item["ebeveyn_enlem"],
            item["ebeveyn_boylam"],
        )
    )
    return parents


def _self_check():
    import numpy as np

    diagonal = np.zeros((7, 7), dtype=bool)
    diagonal[1:3, 1:3] = True
    diagonal[3:5, 3:5] = True
    rows = _child_geometry(
        diagonal,
        [26.30, 38.20, 26.31, 38.21],
        100.0,
        small_site_mask=diagonal,
    )
    assert len(rows) == 1, rows
    children = rows[0]["alt_adaylar"]
    assert len(children) == 2, rows
    assert sorted(item["alan_m2"] for item in children) == [400, 400], rows
    points = {(item["enlem"], item["boylam"]) for item in children}
    assert len(points) == 2, "Alt-kümelerin temsil koordinatları birbirinden ayrışmalı."
    for lat, lon in points:
        assert 38.20 <= lat <= 38.21 and 26.30 <= lon <= 26.31


def audit():
    ensure_daily_schema()
    _self_check()
    now = datetime.now(ISTANBUL)
    report_date = _latest_stored_report_date()
    regions = {}

    if not report_date:
        return {
            "rapor_tarihi": None,
            "olusturma": now.strftime("%Y-%m-%d %H:%M %Z"),
            "alarm": False,
            "saha_gorevi": False,
            "ana_uretim_esigi_m2": satellite.MIN_HOTSPOT_AREA_M2,
            "durum": "kaynak_uydu_raporu_yok",
            "bolgeler": {},
        }

    stored = _stored_snapshot(report_date)
    for region_key in REPORT_REGIONS:
        snapshot = stored.get(region_key, {})
        record = {
            "bolge": satellite.REGIONS[region_key]["label"],
            "son_item": snapshot.get("son_item"),
            "durum": "ok",
            "ebeveynler": [],
        }
        if snapshot.get("hata"):
            record["durum"] = "gunluk_uydu_hatasi"
            record["hata"] = str(snapshot.get("hata"))
            regions[region_key] = record
            continue

        try:
            pair = satellite.sentinel_pair(region_key)
            _, latest = pair
            record["latest_item"] = latest.get("id")
            if snapshot.get("son_item") != latest.get("id"):
                record["durum"] = "gunluk_rapor_latest_ile_eslesmiyor"
                regions[region_key] = record
                continue

            captured = []

            def capture_hotspots(
                change_mask,
                bbox,
                pixel_area_m2,
                small_site_mask=None,
                limit=satellite.HOTSPOT_LIMIT,
                small_quota=satellite.SMALL_HOTSPOT_QUOTA,
            ):
                captured.extend(
                    _child_geometry(
                        change_mask,
                        bbox,
                        pixel_area_m2,
                        small_site_mask=small_site_mask,
                    )
                )
                return _ORIGINAL_HOTSPOTS(
                    change_mask,
                    bbox,
                    pixel_area_m2,
                    small_site_mask=small_site_mask,
                    limit=limit,
                    small_quota=small_quota,
                )

            _analyze_with_hotspot_function(region_key, pair, capture_hotspots)
            record["ebeveynler"] = captured
            record["diyagonal_birlesmis_ebeveyn"] = len(captured)
            record["alt_kume_koordinat_sayisi"] = sum(
                len(item.get("alt_adaylar") or []) for item in captured
            )
        except Exception as exc:
            record["durum"] = "denetim_hatasi"
            record["hata"] = f"{type(exc).__name__}: {exc}"
        regions[region_key] = record

    return {
        "rapor_tarihi": report_date,
        "olusturma": now.strftime("%Y-%m-%d %H:%M %Z"),
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": satellite.MIN_HOTSPOT_AREA_M2,
        "amac": (
            "8-komşulukta yalnız köşeden birleşmiş geçerli Sentinel ebeveynlerinin "
            "4-komşu alt-kümelerine ayrı değişim-pikseli temsil koordinatı vermek."
        ),
        "koordinat_yorumu": (
            "Alt-küme koordinatı değişmiş bir piksel üzerindeki temsil noktasıdır; kesin "
            "parsel/adres değildir ve ana 8-komşu üretim alarmını bölmez."
        ),
        "bolgeler": regions,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print(
            "Diyagonal alt-küme koordinat öz testi başarılı: 250 m² ana eşik ve üretim "
            "8-komşuluğu değişmeden ayrı 4-komşu temsil noktaları hesaplanıyor."
        )
        return

    payload = audit()
    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summaries = []
    for key, item in payload.get("bolgeler", {}).items():
        if item.get("durum") != "ok":
            summaries.append(f"{key}: {item.get('durum')}")
        else:
            summaries.append(
                f"{key}: ebeveyn={item.get('diyagonal_birlesmis_ebeveyn', 0)}, "
                f"alt-koordinat={item.get('alt_kume_koordinat_sayisi', 0)}"
            )
    print("Diyagonal alt-küme koordinat diagnostiği: " + " | ".join(summaries))


if __name__ == "__main__":
    main()
