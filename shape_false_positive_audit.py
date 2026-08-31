"""Sentinel aday geometrisinde geniş/lineer yanlış-pozitif riskini ölçer.

Bu denetim alarm üretmez, saha görevi açmaz ve üretim eşiklerini değiştirmez.
Ana Sentinel motorunun oluşturduğu aynı değişim maskesindeki geçerli 250 m²+ kümelerin
şekil özelliklerini ölçer. Hem ham motor seçimini hem de rebalance + overlap-dedupe
sonrasında günlük raporda gerçekten kalan nihai adayları ayrı ayrı gösterir. Böylece
şekil kalibrasyonu artık üretim öncesi 24'lük ara listeye bakıp yanlış sonuca varmaz.

Rebalance katmanı, geniş bir 8-komşu kümeye yalnız köşeden bağlı 800-10.000 m² parsel
ölçekli yan kümeyi kontrollü olarak ayrı aday yapabilir. Şekil denetimi bu 4-komşu yan
kümeleri de aynı raster üzerinde yeniden kurar; böylece özellikle günlük rotada PARSEL
olarak öne çıkan yan-küme adayları 'geometri çözülemedi' diye yanlış raporlanmaz.

Nihai adaylar, bbox/çözünürlük yeniden analizinden sonra aynı sahne korunmuşsa mevcut
raster geometrisiyle birebir anahtar eşleşmesini kaybedebilir. Bu nedenle tam eşleşmeye
ek olarak yalnız denetim amaçlı 25 m + %60 alan-benzerliği toleranslı bir yaklaşık
eşleşme metriği de üretilir. Yaklaşık eşleşme alarm, görev, öncelik veya filtre kararında
kullanılmaz; yalnız provenans/yeniden-pikselleştirme farkını gerçek geometri kaybından
ayırmaya yardım eder.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

import rebalance_satellite_candidates as rebalance
import satellite
from daily_report import ISTANBUL, REPORT_REGIONS, ensure_daily_schema
from scanner import connect


AUDIT_FILE = Path(__file__).with_name("shape_false_positive_audit.json")
LONG_THIN_ASPECT_MIN = 5.0
LOW_COMPACTNESS_MAX = 0.15
FINAL_APPROX_MATCH_METERS = 25
FINAL_APPROX_MIN_AREA_SIMILARITY = 0.60
EXAMPLE_LIMIT = 6
_ORIGINAL_HOTSPOTS = satellite._hotspots


def _area_bucket(area_m2):
    if area_m2 < satellite.SMALL_HOTSPOT_MAX_M2:
        return "kucuk_250_800"
    if area_m2 <= 10_000:
        return "santiye_olcegi_800_10000"
    return "genis_10000_ustu"


def _eligible(component, pixel_area_m2, small_site_mask=None):
    area_m2 = len(component) * pixel_area_m2
    if area_m2 < satellite.MIN_HOTSPOT_AREA_M2:
        return False
    if area_m2 >= satellite.SMALL_HOTSPOT_MAX_M2:
        return True
    if small_site_mask is None:
        return False
    pixels = np.asarray(component, dtype="int32")
    strong_fraction = float(
        np.mean(small_site_mask[pixels[:, 0], pixels[:, 1]])
    )
    return strong_fraction >= 0.50


def _component_location(component, bbox, shape):
    pixels = np.asarray(component, dtype="int32")
    centroid = pixels.mean(axis=0)
    distance = np.sum((pixels - centroid) ** 2, axis=1)
    representative = pixels[int(np.argmin(distance))]
    row, column = int(representative[0]), int(representative[1])
    height, width = shape
    west, south, east, north = bbox
    latitude = north - (row + 0.5) / height * (north - south)
    longitude = west + (column + 0.5) / width * (east - west)
    return round(latitude, 6), round(longitude, 6)


def _perimeter_edges(component):
    pixels = set(component)
    exposed = 0
    for row, column in component:
        exposed += (row - 1, column) not in pixels
        exposed += (row + 1, column) not in pixels
        exposed += (row, column - 1) not in pixels
        exposed += (row, column + 1) not in pixels
    return max(int(exposed), 1)


def _shape_record(component, bbox, shape, pixel_area_m2):
    pixels = np.asarray(component, dtype="int32")
    rows = pixels[:, 0]
    columns = pixels[:, 1]
    row_span = int(rows.max() - rows.min() + 1)
    col_span = int(columns.max() - columns.min() + 1)
    short_span = max(min(row_span, col_span), 1)
    long_span = max(row_span, col_span)
    aspect_ratio = float(long_span / short_span)
    fill_ratio = float(len(component) / max(row_span * col_span, 1))
    perimeter = _perimeter_edges(component)
    compactness = float(4 * math.pi * len(component) / (perimeter * perimeter))
    latitude, longitude = _component_location(component, bbox, shape)
    area_m2 = round(len(component) * pixel_area_m2)
    return {
        "enlem": latitude,
        "boylam": longitude,
        "alan_m2": area_m2,
        "olcek": _area_bucket(area_m2),
        "piksel": len(component),
        "satir_span": row_span,
        "sutun_span": col_span,
        "uzun_kisa_orani": round(aspect_ratio, 2),
        "kutu_doluluk_orani": round(fill_ratio, 3),
        "kompaktlik": round(compactness, 3),
        "uzun_ince": aspect_ratio >= LONG_THIN_ASPECT_MIN,
        "dusuk_kompaktlik": compactness <= LOW_COMPACTNESS_MAX,
    }


def _candidate_key(item):
    try:
        return (
            round(float(item.get("enlem")), 6),
            round(float(item.get("boylam")), 6),
            round(float(item.get("alan_m2") or 0)),
        )
    except (TypeError, ValueError, AttributeError):
        return None


def _sidecar_shape_records(change_mask, bbox, pixel_area_m2, small_site_mask=None):
    """Rebalance'ın 4-komşu yan-küme adaylarını şekil kaydına dönüştür."""
    sidecars = rebalance._diagonal_sidecar_candidates(
        change_mask,
        bbox,
        pixel_area_m2,
        small_site_mask=small_site_mask,
    )
    keys = {key for key in map(_candidate_key, sidecars) if key is not None}
    if not keys:
        return []

    records = []
    for component in rebalance._four_connected_components(change_mask):
        record = _shape_record(
            component,
            bbox,
            change_mask.shape,
            pixel_area_m2,
        )
        if _candidate_key(record) not in keys:
            continue
        record["geometri_kaynagi"] = rebalance.DIAGONAL_SIDECAR_TAG
        records.append(record)
    return records


def _number(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError, AttributeError):
        return default


def _distance_m(first, second):
    lat1 = _number(first.get("enlem"))
    lon1 = _number(first.get("boylam"))
    lat2 = _number(second.get("enlem"))
    lon2 = _number(second.get("boylam"))
    if None in (lat1, lon1, lat2, lon2):
        return None
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _area_similarity(first, second):
    first_area = _number(first.get("alan_m2"), 0) or 0
    second_area = _number(second.get("alan_m2"), 0) or 0
    larger = max(first_area, second_area)
    if larger <= 0:
        return 0.0
    return min(first_area, second_area) / larger


def _match_records(records, candidates):
    keys = {key for key in map(_candidate_key, candidates or []) if key is not None}
    return [record for record in records if _candidate_key(record) in keys]


def _approximate_match_records(
    records,
    candidates,
    max_distance_m=FINAL_APPROX_MATCH_METERS,
    min_area_similarity=FINAL_APPROX_MIN_AREA_SIMILARITY,
):
    """Tam eşleşmeyen nihai adayları yalnız denetim için yakın geometriyle eşleştir.

    Her raster kaydı en fazla bir nihai adaya atanır. Bu eşleşme operasyonel değildir;
    bbox/çözünürlük yeniden pikselleştirmesi nedeniyle oluşan küçük centroid/alan
    oynamalarını ölçmek içindir.
    """
    exact_keys = {key for key in map(_candidate_key, candidates or []) if key is not None}
    used_record_indexes = {
        index
        for index, record in enumerate(records)
        if _candidate_key(record) in exact_keys
    }
    exact_candidate_keys = {
        _candidate_key(record)
        for index, record in enumerate(records)
        if index in used_record_indexes
    }

    approximate_records = []
    diagnostics = []
    for candidate in candidates or []:
        candidate_key = _candidate_key(candidate)
        if candidate_key is None or candidate_key in exact_candidate_keys:
            continue

        best_index = None
        best_distance = None
        best_similarity = 0.0
        nearest_distance = None
        nearest_similarity = 0.0
        for index, record in enumerate(records):
            distance = _distance_m(record, candidate)
            if distance is None:
                continue
            similarity = _area_similarity(record, candidate)
            if nearest_distance is None or distance < nearest_distance:
                nearest_distance = distance
                nearest_similarity = similarity
            if index in used_record_indexes:
                continue
            if distance > max_distance_m or similarity < min_area_similarity:
                continue
            if (
                best_distance is None
                or distance < best_distance
                or (
                    abs(distance - best_distance) < 0.01
                    and similarity > best_similarity
                )
            ):
                best_index = index
                best_distance = distance
                best_similarity = similarity

        if best_index is not None:
            used_record_indexes.add(best_index)
            approximate_records.append(records[best_index])
            diagnostics.append(
                {
                    "enlem": _number(candidate.get("enlem")),
                    "boylam": _number(candidate.get("boylam")),
                    "alan_m2": round(_number(candidate.get("alan_m2"), 0) or 0),
                    "durum": "yaklasik_eslesti",
                    "mesafe_m": round(best_distance, 1),
                    "alan_benzerligi": round(best_similarity, 3),
                }
            )
        else:
            diagnostics.append(
                {
                    "enlem": _number(candidate.get("enlem")),
                    "boylam": _number(candidate.get("boylam")),
                    "alan_m2": round(_number(candidate.get("alan_m2"), 0) or 0),
                    "durum": "eslesmedi",
                    "geometri_kaynagi": candidate.get("geometri_kaynagi"),
                    "en_yakin_mesafe_m": (
                        round(nearest_distance, 1)
                        if nearest_distance is not None
                        else None
                    ),
                    "en_yakin_alan_benzerligi": round(nearest_similarity, 3),
                }
            )

    return approximate_records, diagnostics


def _flagged_wide(records):
    return [
        record
        for record in records
        if record["olcek"] == "genis_10000_ustu"
        and (record["uzun_ince"] or record["dusuk_kompaktlik"])
    ]


def _examples(records):
    return sorted(
        records,
        key=lambda item: (
            not item["uzun_ince"],
            not item["dusuk_kompaktlik"],
            -item["alan_m2"],
        ),
    )[:EXAMPLE_LIMIT]


def _summarize(records):
    buckets = {
        "kucuk_250_800": {"toplam": 0, "uzun_ince": 0, "dusuk_kompaktlik": 0},
        "santiye_olcegi_800_10000": {"toplam": 0, "uzun_ince": 0, "dusuk_kompaktlik": 0},
        "genis_10000_ustu": {"toplam": 0, "uzun_ince": 0, "dusuk_kompaktlik": 0},
    }
    for record in records:
        bucket = buckets[record["olcek"]]
        bucket["toplam"] += 1
        bucket["uzun_ince"] += int(bool(record.get("uzun_ince")))
        bucket["dusuk_kompaktlik"] += int(bool(record.get("dusuk_kompaktlik")))
    return buckets


def _self_check():
    square = [(r, c) for r in range(4) for c in range(4)]
    long_strip = [(r, c) for r in range(2) for c in range(12)]
    bent = [(0, c) for c in range(8)] + [(r, 7) for r in range(1, 8)]
    bbox = [26.30, 38.20, 26.32, 38.22]
    shape = (20, 20)

    square_record = _shape_record(square, bbox, shape, 100.0)
    strip_record = _shape_record(long_strip, bbox, shape, 100.0)
    bent_record = _shape_record(bent, bbox, shape, 100.0)

    assert not square_record["uzun_ince"], square_record
    assert strip_record["uzun_ince"], strip_record
    assert square_record["kompaktlik"] > bent_record["kompaktlik"], (
        square_record,
        bent_record,
    )
    assert _area_bucket(500) == "kucuk_250_800"
    assert _area_bucket(5000) == "santiye_olcegi_800_10000"
    assert _area_bucket(15000) == "genis_10000_ustu"
    assert _match_records(
        [square_record, strip_record],
        [{"enlem": square_record["enlem"], "boylam": square_record["boylam"], "alan_m2": square_record["alan_m2"]}],
    ) == [square_record]

    shifted_candidate = {
        "enlem": square_record["enlem"] + 0.00005,
        "boylam": square_record["boylam"] + 0.00005,
        "alan_m2": round(square_record["alan_m2"] * 0.92),
    }
    approximate, diagnostics = _approximate_match_records(
        [square_record],
        [shifted_candidate],
    )
    assert approximate == [square_record], diagnostics
    assert diagnostics[0]["durum"] == "yaklasik_eslesti", diagnostics
    assert diagnostics[0]["mesafe_m"] < FINAL_APPROX_MATCH_METERS, diagnostics

    far_candidate = dict(
        shifted_candidate,
        enlem=shifted_candidate["enlem"] + 0.001,
    )
    approximate, diagnostics = _approximate_match_records(
        [square_record],
        [far_candidate],
    )
    assert not approximate, diagnostics
    assert diagnostics[0]["durum"] == "eslesmedi", diagnostics

    sidecar_mask = np.zeros((20, 20), dtype=bool)
    sidecar_mask[2:13, 2:12] = True
    sidecar_mask[13:15, 12:16] = True
    sidecar_records = _sidecar_shape_records(
        sidecar_mask,
        bbox,
        100.0,
    )
    assert len(sidecar_records) == 1, sidecar_records
    assert sidecar_records[0]["alan_m2"] == 800, sidecar_records
    assert (
        sidecar_records[0].get("geometri_kaynagi")
        == rebalance.DIAGONAL_SIDECAR_TAG
    ), sidecar_records


def _today_snapshot(connection, report_date, region_key):
    row = connection.execute(
        """SELECT son_item,hareket_json,hata FROM gunluk_uydu_raporlari
        WHERE rapor_tarihi=? AND bolge=? LIMIT 1""",
        (report_date, region_key),
    ).fetchone()
    if not row:
        return {"son_item": None, "hareket": [], "hata": "rapor_yok"}
    try:
        movement = json.loads(row[1] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        movement = []
    if not isinstance(movement, list):
        movement = []
    return {
        "son_item": row[0],
        "hareket": [item for item in movement if isinstance(item, dict)],
        "hata": row[2],
    }


def _analyze_region(region_key, pair, final_candidates=None):
    captured = {
        "base_records": [],
        "sidecar_records": [],
        "records": [],
        "selected": [],
    }

    def audited_hotspots(
        change_mask,
        bbox,
        pixel_area_m2,
        small_site_mask=None,
        limit=satellite.HOTSPOT_LIMIT,
        small_quota=satellite.SMALL_HOTSPOT_QUOTA,
    ):
        base_records = []
        for component in satellite._connected_components(change_mask):
            if not _eligible(component, pixel_area_m2, small_site_mask):
                continue
            base_records.append(
                _shape_record(
                    component,
                    bbox,
                    change_mask.shape,
                    pixel_area_m2,
                )
            )
        sidecar_records = _sidecar_shape_records(
            change_mask,
            bbox,
            pixel_area_m2,
            small_site_mask=small_site_mask,
        )
        selected = _ORIGINAL_HOTSPOTS(
            change_mask,
            bbox,
            pixel_area_m2,
            small_site_mask=small_site_mask,
            limit=limit,
            small_quota=small_quota,
        )
        captured["base_records"] = base_records
        captured["sidecar_records"] = sidecar_records
        captured["records"] = base_records + sidecar_records
        captured["selected"] = [item for item in selected if isinstance(item, dict)]
        return selected

    original = satellite._hotspots
    satellite._hotspots = audited_hotspots
    try:
        satellite.analyze_sentinel_change(region_key, pair=pair)
    finally:
        satellite._hotspots = original

    final_candidates = final_candidates or []
    raw_selected_records = _match_records(
        captured["base_records"],
        captured["selected"],
    )
    final_selected_records = _match_records(captured["records"], final_candidates)
    approximate_records, match_diagnostics = _approximate_match_records(
        captured["records"],
        final_candidates,
    )
    resolved_records = final_selected_records + approximate_records

    raw_flagged = _flagged_wide(raw_selected_records)
    final_flagged = _flagged_wide(final_selected_records)
    resolved_flagged = _flagged_wide(resolved_records)
    approximate_match_count = sum(
        1 for item in match_diagnostics if item.get("durum") == "yaklasik_eslesti"
    )
    unresolved = [
        item for item in match_diagnostics if item.get("durum") == "eslesmedi"
    ]
    exact_sidecars = [
        record
        for record in final_selected_records
        if record.get("geometri_kaynagi") == rebalance.DIAGONAL_SIDECAR_TAG
    ]

    return {
        "ham_gecerli_aday": len(captured["base_records"]),
        "diyagonal_yan_kume_geometri": len(captured["sidecar_records"]),
        # Geriye uyumluluk: eski alanlar ham satellite._hotspots seçimini gösterir.
        "uretim_secimi": len(captured["selected"]),
        "ham_sekil_dagilimi": _summarize(captured["base_records"]),
        "uretim_secimi_sekil_dagilimi": _summarize(raw_selected_records),
        "secili_genis_sekil_isaretli": len(raw_flagged),
        "secili_genis_sekil_ornekleri": _examples(raw_flagged),
        # Asıl operasyonel çıktı: rebalance + dedupe sonrasında DB'de kalan adaylar.
        "nihai_rapor_secimi": len(final_candidates),
        "nihai_rapor_eslesen_geometri": len(final_selected_records),
        "nihai_rapor_diyagonal_yan_kume_eslesen_geometri": len(exact_sidecars),
        "nihai_rapor_yaklasik_eslesen_geometri": approximate_match_count,
        "nihai_rapor_cozulen_geometri": len(resolved_records),
        "nihai_rapor_cozulemeyen_geometri": len(unresolved),
        "nihai_rapor_yaklasik_esleme_esigi_m": FINAL_APPROX_MATCH_METERS,
        "nihai_rapor_yaklasik_min_alan_benzerligi": FINAL_APPROX_MIN_AREA_SIMILARITY,
        "nihai_rapor_secimi_sekil_dagilimi": _summarize(final_selected_records),
        "nihai_rapor_secimi_sekil_dagilimi_yaklasik_dahil": _summarize(
            resolved_records
        ),
        "nihai_secili_genis_sekil_isaretli": len(final_flagged),
        "nihai_secili_genis_sekil_isaretli_yaklasik_dahil": len(resolved_flagged),
        "nihai_secili_genis_sekil_ornekleri": _examples(final_flagged),
        "nihai_cozulemeyen_geometri_ornekleri": unresolved[:EXAMPLE_LIMIT],
    }


def audit_shapes():
    ensure_daily_schema()
    _self_check()
    now = datetime.now(ISTANBUL)
    report_date = now.strftime("%Y-%m-%d")
    regions = {}

    with connect() as connection:
        for region_key in REPORT_REGIONS:
            snapshot = _today_snapshot(connection, report_date, region_key)
            record = {
                "bolge": satellite.REGIONS[region_key]["label"],
                "son_item": snapshot.get("son_item"),
                "durum": "ok",
            }
            if snapshot.get("hata"):
                record["durum"] = "gunluk_uydu_hatasi"
                record["hata"] = str(snapshot.get("hata"))
                regions[region_key] = record
                continue
            try:
                pair = satellite.sentinel_pair(region_key)
                latest_item = pair[1].get("id")
                record["latest_item"] = latest_item
                if not snapshot.get("son_item") or snapshot.get("son_item") != latest_item:
                    record["durum"] = "gunluk_rapor_latest_ile_eslesmiyor"
                    regions[region_key] = record
                    continue
                record.update(
                    _analyze_region(
                        region_key,
                        pair,
                        final_candidates=snapshot.get("hareket", []),
                    )
                )
            except Exception as exc:
                record["durum"] = "denetim_hatasi"
                record["hata"] = f"{type(exc).__name__}: {exc}"
            regions[region_key] = record

    payload = {
        "rapor_tarihi": report_date,
        "olusturma": now.strftime("%Y-%m-%d %H:%M %Z"),
        "amac": (
            "Üretim alarmını değiştirmeden 250 m²+ Sentinel kümelerinde uzun-ince ve "
            "düşük-kompaktlık geometrisini ölçmek; ham motor seçimini ve rebalance + "
            "dedupe sonrası gerçekten sahaya kalabilen nihai adayları ayrı izlemek."
        ),
        "esikler": {
            "uzun_ince_min_uzun_kisa_orani": LONG_THIN_ASPECT_MIN,
            "dusuk_kompaktlik_max": LOW_COMPACTNESS_MAX,
            "nihai_yaklasik_esleme_max_m": FINAL_APPROX_MATCH_METERS,
            "nihai_yaklasik_esleme_min_alan_benzerligi": (
                FINAL_APPROX_MIN_AREA_SIMILARITY
            ),
        },
        "uyari": (
            "Şekil etiketi tek başına yol veya yanlış pozitif kanıtı değildir; hiçbir aday "
            "bu denetim nedeniyle elenmez veya öncelik kaybetmez. 4-komşu yan-küme "
            "geometrisi yalnız mevcut rebalance adayının provenansını doğrular. Yaklaşık "
            "eşleşme yalnız bbox/çözünürlük yeniden-pikselleştirme provenansını ölçer ve "
            "alarm/rota kararında kullanılmaz."
        ),
        "bolgeler": regions,
    }
    AUDIT_FILE.write_text(
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
        print(
            "Şekil yanlış-pozitif denetimi kalite kontrolü başarılı: ham, 4-komşu "
            "yan-küme, nihai tam eşleşme ve yalnız-denetim yaklaşık geometri "
            "eşleşmesi ölçülebiliyor."
        )
        return

    payload = audit_shapes()
    summaries = []
    for key, item in payload["bolgeler"].items():
        if item.get("durum") != "ok":
            summaries.append(f"{key}: {item.get('durum')}")
            continue
        raw_selected = item.get("uretim_secimi_sekil_dagilimi", {})
        raw_wide = raw_selected.get("genis_10000_ustu", {})
        final_selected = item.get(
            "nihai_rapor_secimi_sekil_dagilimi_yaklasik_dahil", {}
        )
        final_wide = final_selected.get("genis_10000_ustu", {})
        summaries.append(
            f"{key}: ham >10000={raw_wide.get('toplam', 0)} / "
            f"şekil-işaretli={item.get('secili_genis_sekil_isaretli', 0)}; "
            f"nihai >10000={final_wide.get('toplam', 0)} / "
            f"şekil-işaretli="
            f"{item.get('nihai_secili_genis_sekil_isaretli_yaklasik_dahil', 0)}; "
            f"tam={item.get('nihai_rapor_eslesen_geometri', 0)} "
            f"(yan-küme={item.get('nihai_rapor_diyagonal_yan_kume_eslesen_geometri', 0)}) + "
            f"yaklaşık={item.get('nihai_rapor_yaklasik_eslesen_geometri', 0)} / "
            f"{item.get('nihai_rapor_secimi', 0)}; "
            f"çözülemeyen={item.get('nihai_rapor_cozulemeyen_geometri', 0)}"
        )
    print("Şekil yanlış-pozitif denetimi: " + " | ".join(summaries))


if __name__ == "__main__":
    main()
