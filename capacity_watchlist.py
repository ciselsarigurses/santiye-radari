"""Mevcut Sentinel sahnesinde 24-aday tavanının dışında kalan şantiye ölçeği adaylardan saha diagnostik devriyesi üretir.

Bu katman alarm üretmez, saha görevi açmaz, 250 m²/10 m/spektral eşikleri değiştirmez
ve geçmiş bir Sentinel sahnesini geriye dönük olarak üretim listesine sokmaz. Yalnız,
aynı üretim değişim maskesini geçmiş fakat ana 24-aday tavanının dışında kalmış
800-10.000 m² adaylardan, mevcut saha görevlerinden ve seçilmiş üretim adaylarından
uzakta kalan en fazla iki koordinatı günlük rapora "kapasite diagnostik" olarak ekler.

Amaç, yeni Sentinel sahnesinde devreye girecek dengeli şantiye kotası gelene kadar
mevcut sahnenin ölçülmüş kapasite körlüğünden saha kalibrasyonu için güvenli örnek
toplamaktır. Bir bölgede üretim listesi zaten en az 8 şantiye ölçeği aday içeriyorsa
bu ek diagnostik o bölge için kapanır.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime
from pathlib import Path

import rebalance_satellite_candidates as rebalance
import satellite
from daily_report import ISTANBUL, REPORT_REGIONS, ensure_daily_schema
from scanner import connect


REPORT_JSON = Path(__file__).with_name("latest_report.json")
REPORT_MD = Path(__file__).with_name("SAHA_RAPORU.md")

CONSTRUCTION_MIN_M2 = rebalance.CONSTRUCTION_SCALE_MIN_M2
CONSTRUCTION_MAX_M2 = rebalance.CONSTRUCTION_SCALE_MAX_M2
EARLY_MAX_M2 = rebalance.EARLY_PARCEL_MAX_M2
TARGET_PRODUCTION_QUOTA = 8

MIN_ACTIVE_DISTANCE_M = 150.0
MIN_SELECTED_DISTANCE_M = 100.0
MIN_WATCHLIST_DISTANCE_M = 500.0
MAX_TOTAL = 2

SECTION_START = "<!-- capacity-watchlist:start -->"
SECTION_END = "<!-- capacity-watchlist:end -->"


def _area(item):
    try:
        return max(float(item.get("alan_m2") or 0), 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _point(item):
    try:
        return float(item.get("enlem")), float(item.get("boylam"))
    except (TypeError, ValueError, AttributeError):
        return None


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _candidate_key(item):
    return rebalance._candidate_key(item)


def _construction_count(items):
    return sum(
        CONSTRUCTION_MIN_M2 <= _area(item) <= CONSTRUCTION_MAX_M2
        for item in items
        if isinstance(item, dict)
    )


def _load_report():
    if not REPORT_JSON.exists():
        return {}
    try:
        payload = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _active_points(report):
    points = []
    for item in report.get("saha_adaylari", []):
        if not isinstance(item, dict):
            continue
        point = _point(item)
        if point is not None:
            points.append(point)
    return points


def _stored_snapshot(report_date):
    snapshots = {}
    with connect() as connection:
        for region_key in REPORT_REGIONS:
            row = connection.execute(
                """SELECT son_item,hareket_json,hata FROM gunluk_uydu_raporlari
                WHERE rapor_tarihi=? AND bolge=? LIMIT 1""",
                (report_date, region_key),
            ).fetchone()
            if not row:
                snapshots[region_key] = {
                    "son_item": None,
                    "hareket": [],
                    "hata": "rapor_yok",
                }
                continue
            try:
                movement = json.loads(row[1] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                movement = []
            if not isinstance(movement, list):
                movement = []
            snapshots[region_key] = {
                "son_item": row[0],
                "hareket": [item for item in movement if isinstance(item, dict)],
                "hata": row[2],
            }
    return snapshots


def _rank_key(item):
    area = _area(item)
    strength = rebalance._signal_strength(item)
    return (
        0 if strength > 0 else 1,
        0 if area <= EARLY_MAX_M2 else 1,
        -strength,
        area,
        float(item.get("enlem") or 0),
        float(item.get("boylam") or 0),
    )


def _far_enough(candidate, active_points, selected_points, watch_points):
    point = _point(candidate)
    if point is None:
        return False
    if any(_distance_m(point, other) < MIN_ACTIVE_DISTANCE_M for other in active_points):
        return False
    if any(_distance_m(point, other) < MIN_SELECTED_DISTANCE_M for other in selected_points):
        return False
    if any(_distance_m(point, other) < MIN_WATCHLIST_DISTANCE_M for other in watch_points):
        return False
    return True


def _candidate_to_watch(region_key, candidate, pair):
    older, latest = pair
    latitude, longitude = _point(candidate)
    return {
        "oncelik": "KAPASITE_DIAGNOSTIK",
        "mahalle": candidate.get("mahalle") or "Mevki doğrulanmadı",
        "enlem": round(latitude, 6),
        "boylam": round(longitude, 6),
        "alan_m2": round(_area(candidate)),
        "bolge": satellite.REGIONS[region_key]["label"],
        "onceki_tarih": satellite._item_date(older),
        "son_tarih": satellite._item_date(latest),
        "guclu_sinyal_orani": round(rebalance._signal_strength(candidate), 4),
        "geometri_kaynagi": candidate.get("geometri_kaynagi"),
        "alarm": False,
        "saha_gorevi": False,
        "harita": (
            "https://www.google.com/maps/dir/?api=1&destination="
            f"{latitude:.6f},{longitude:.6f}"
        ),
        "neden": (
            "Aynı 250 m²+ / yaklaşık 10 m Sentinel değişim maskesini geçti; "
            "ana 24-aday tavanının dışında kaldığı için üretim alarmı değildir. "
            "Mevcut saha görevlerinden uzakta, şantiye ölçeği kapasite kalibrasyonu "
            "için seçildi."
        ),
    }


def _remove_section(text):
    pattern = re.compile(
        re.escape(SECTION_START) + r".*?" + re.escape(SECTION_END) + r"\n*",
        re.DOTALL,
    )
    return pattern.sub("", text)


def _render_section(items, diagnostics):
    if not items:
        return ""
    lines = [
        SECTION_START,
        "## Aday tavanı şantiye ölçeği devriyesi",
        "",
        (
            "> **Alarm veya saha görevi değildir.** Aynı 250 m²+ / yaklaşık 10 m "
            "Sentinel üretim maskesini geçmiş, fakat ana 24-aday kapasitesinin dışında "
            "kalmış 800–10.000 m² adaylardan seçilir. Mevcut görevlerden uzakta en fazla "
            "iki nokta gösterilir. Amaç mevcut sahnedeki ölçülmüş kapasite körlüğünü "
            "sahada kalibre etmektir; üretim eşikleri ve alarm sayısı değişmez."
        ),
        "",
    ]
    for index, item in enumerate(items, 1):
        strength = item.get("guclu_sinyal_orani", 0)
        lines.append(
            f"{index}. **KAPASİTE — {item.get('mahalle', 'Mevki doğrulanmadı')}** · "
            f"yaklaşık {int(item.get('alan_m2') or 0):,} m² · "
            f"{item.get('onceki_tarih')} → {item.get('son_tarih')} · "
            f"güçlü-piksel oranı {strength:.2f} · "
            f"[Yol tarifi]({item.get('harita')})"
        )
        lines.append(
            "   - Saha notu: Kazı/temel/şantiye varsa fotoğraf ve kısa not al; "
            "tarla sürümü, yol, bahçe temizliği veya başka neden ise onu yaz."
        )
    if diagnostics:
        summaries = []
        for key in REPORT_REGIONS:
            record = diagnostics.get(key)
            if not record:
                continue
            summaries.append(
                f"{key}: üretimde {record.get('uretim_santiye_olcegi', 0)}, "
                f"tavan dışında uygun havuz {record.get('tavan_disinda_uygun_havuz', 0)}"
            )
        if summaries:
            lines.extend(["", "_Kapasite özeti: " + " · ".join(summaries) + "_"])
    lines.extend([SECTION_END, ""])
    return "\n".join(lines)


def _write_reports(report, items, diagnostics):
    report["kapasite_santiye_devriyesi"] = items
    report["kapasite_santiye_devriyesi_meta"] = {
        "alarm": False,
        "saha_gorevi": False,
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "bolgeler": diagnostics,
    }
    REPORT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if not REPORT_MD.exists():
        return
    text = _remove_section(REPORT_MD.read_text(encoding="utf-8"))
    section = _render_section(items, diagnostics)
    anchor = "## Bugün sahada kontrol edilecek uydu adayları"
    if section and anchor in text:
        text = text.replace(anchor, section + "\n" + anchor, 1)
    elif section:
        text = text.rstrip() + "\n\n" + section
    REPORT_MD.write_text(text, encoding="utf-8")


def _self_check():
    early = {"enlem": 38.20, "boylam": 26.30, "alan_m2": 1200, rebalance.STRONG_SIGNAL_FIELD: 0.8}
    upper = {"enlem": 38.21, "boylam": 26.31, "alan_m2": 4200, rebalance.STRONG_SIGNAL_FIELD: 0.9}
    zero_signal = {"enlem": 38.205, "boylam": 26.305, "alan_m2": 1000, rebalance.STRONG_SIGNAL_FIELD: 0.0}
    small = {"enlem": 38.22, "boylam": 26.32, "alan_m2": 500}
    assert _rank_key(early) < _rank_key(upper), "Pozitif güçlü-sinyalli erken-parsel sınıfı önce gelmeli."
    assert _rank_key(upper) < _rank_key(zero_signal), "Sıfır güçlü-sinyalli aday, pozitif kanıtlı adayın önüne geçmemeli."
    assert _construction_count([early, upper, small]) == 2
    assert _distance_m((38.20, 26.30), (38.20, 26.30)) == 0
    assert not _far_enough(
        early,
        [(38.2002, 26.3002)],
        [],
        [],
    ), "Yakın aktif görev diagnostik devriyeye girmemeli."
    sample = (
        "baş\n"
        + SECTION_START
        + "\neski\n"
        + SECTION_END
        + "\n## Bugün sahada kontrol edilecek uydu adayları\n"
    )
    cleaned = _remove_section(sample)
    assert "eski" not in cleaned and "Bugün sahada" in cleaned


def build_capacity_watchlist():
    ensure_daily_schema()
    _self_check()
    now = datetime.now(ISTANBUL)
    report_date = now.strftime("%Y-%m-%d")
    report = _load_report()
    if report.get("rapor_tarihi") != report_date:
        return None, {"durum": "guncel_rapor_yok"}

    snapshots = _stored_snapshot(report_date)
    active_points = _active_points(report)
    chosen = []
    watch_points = []
    diagnostics = {}
    successful_regions = 0

    for region_key in REPORT_REGIONS:
        snapshot = snapshots.get(region_key, {})
        record = {
            "durum": "ok",
            "uretim_santiye_olcegi": _construction_count(snapshot.get("hareket", [])),
            "tavan_disinda_uygun_havuz": 0,
        }
        if snapshot.get("hata") or not snapshot.get("son_item"):
            record["durum"] = "gunluk_uydu_hatasi"
            diagnostics[region_key] = record
            continue

        if record["uretim_santiye_olcegi"] >= TARGET_PRODUCTION_QUOTA:
            record["durum"] = "uretim_kotasi_yeterli"
            diagnostics[region_key] = record
            successful_regions += 1
            continue

        try:
            pair = satellite.sentinel_pair(region_key)
            if pair[1].get("id") != snapshot.get("son_item"):
                record["durum"] = "gunluk_rapor_latest_ile_eslesmiyor"
                diagnostics[region_key] = record
                continue

            capped_result = satellite.analyze_sentinel_change(region_key, pair=pair)
            raw_result = rebalance._uncapped_analysis(region_key, pair)
            capped = [
                item for item in capped_result.get("hotspots", [])
                if isinstance(item, dict)
            ]
            raw = [
                item for item in raw_result.get("hotspots", [])
                if isinstance(item, dict)
            ]
            capped_keys = {
                key for key in map(_candidate_key, capped) if key is not None
            }
            selected_points = [
                point
                for item in snapshot.get("hareket", [])
                if (point := _point(item)) is not None
            ]

            pool = []
            for candidate in raw:
                area = _area(candidate)
                key = _candidate_key(candidate)
                if not (
                    CONSTRUCTION_MIN_M2 <= area <= CONSTRUCTION_MAX_M2
                    and key is not None
                    and key not in capped_keys
                    and candidate.get("geometri_kaynagi") != rebalance.DIAGONAL_SIDECAR_TAG
                ):
                    continue
                if not _far_enough(
                    candidate,
                    active_points,
                    selected_points,
                    watch_points,
                ):
                    continue
                pool.append(candidate)

            pool = sorted(pool, key=_rank_key)
            record["tavan_disinda_uygun_havuz"] = len(pool)
            successful_regions += 1
            if pool and len(chosen) < MAX_TOTAL:
                item = _candidate_to_watch(region_key, pool[0], pair)
                chosen.append(item)
                watch_points.append((item["enlem"], item["boylam"]))
        except Exception as exc:
            record["durum"] = "denetim_hatasi"
            record["hata"] = f"{type(exc).__name__}: {exc}"
        diagnostics[region_key] = record

    if successful_regions == 0:
        return None, {
            "durum": "tum_bolgeler_gecici_hata",
            "bolgeler": diagnostics,
        }

    _write_reports(report, chosen[:MAX_TOTAL], diagnostics)
    return chosen[:MAX_TOTAL], {
        "durum": "ok",
        "bolgeler": diagnostics,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print(
            "Kapasite şantiye devriyesi kalite kontrolü başarılı: alarm/görev üretmiyor; "
            "yalnız 24-aday tavanı dışındaki 800-10.000 m² aynı-mask adaylarını "
            "mevcut görevlerden uzakta seçiyor."
        )
        return

    items, info = build_capacity_watchlist()
    if items is None:
        print("Kapasite şantiye devriyesi rapora dokunmadı: " + str(info.get("durum")))
        return
    if not items:
        print("Kapasite şantiye devriyesi: güvenli ek saha noktası yok veya üretim kotası yeterli.")
        return
    summary = " | ".join(
        f"{item['mahalle']} {item['alan_m2']} m² @ {item['enlem']},{item['boylam']}"
        for item in items
    )
    print("Kapasite şantiye devriyesi eklendi: " + summary)


if __name__ == "__main__":
    main()
