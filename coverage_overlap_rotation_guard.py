"""Çeşme-Uzunkuyu ortak Sentinel şeridinin kör-alan devriyesinde aç kalmasını önler.

``route_diversity_guard`` iki üretim kutusunun yalnız kendilerinin gördüğü çekirdek
alanları tercih eder. Bu, doğu/batı çekirdeğinin örtüşme yüzünden atlanmasını çözer;
ancak her iki çekirdekte de güvenli aday bulunduğu sürece 26.45-26.53 E ortak şeridi
insan devriyesine hiç gelemeyebilir.

Bu son koruma alarm, görev veya nokta sayısını artırmaz. Üç günlük dönüşüm kullanır:
bir gün Çeşme tarafındaki kör-alan slotu ortak şeritten seçilir, bir gün mevcut iki
çekirdek seçimi aynen korunur, bir gün Uzunkuyu tarafındaki slot ortak şeritten seçilir.
Örtüşme adayı yalnız mevcut 250 m²+, aktif görevden >=150 m ve diğer devriye noktasından
>=250 m güvenliklerini geçerse kullanılır. Güvenli aday yoksa iki-çekirdek davranışına
geri dönülür. Böylece çekirdek koruması sürerken ortak şerit kalıcı biçimde aç kalmaz.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import coverage_patrol_shortlist as coverage
import route_diversity_guard as route
import satellite


AUDIT_JSON = Path(__file__).with_name("coverage_blind_area_audit.json")
REPORT_JSON = Path(__file__).with_name("latest_report.json")
FIELD_REPORT_MD = Path(__file__).with_name("SAHA_RAPORU.md")
ROTATION_REGIONS = ("cesme", "uzunkuyu")
NOTE = (
    "Alarm/görev değildir; tarihsel Sentinel verisinde kara olduğu doğrulanmış kalıcı "
    "gözlem boşluklarından günlük en fazla iki insan kontrol noktası seçilir. Aktif "
    "radar görevlerinden >=150 m, birbirinden >=250 m uzakta tutulur. Çeşme ve "
    "Uzunkuyu çekirdekleri korunurken ortak Sentinel şeridi kalıcı biçimde aç kalmasın "
    "diye Çeşme yerel takvim gününe bağlı üç günlük dönüşüm uygulanır: bir gün Çeşme "
    "slotu ortak şeridi örnekler, bir gün iki çekirdek aynen korunur, bir gün Uzunkuyu "
    "slotu ortak şeridi örnekler. Güvenli örtüşme adayı yoksa mevcut çekirdek seçimine "
    "geri dönülür; kuru-zemin kalibrasyonunda temsil edilen mahalleler güvenli alternatif "
    "varsa tekrar seçilmez."
)


def _report_day(report_payload):
    raw = str((report_payload or {}).get("rapor_tarihi") or "")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _rotation_target(report_payload, rotation_day=None):
    """Üç günde bir iki çekirdeği koru; diğer iki günde hedef bölgeyi sırayla döndür."""
    day = rotation_day or _report_day(report_payload)
    if day is None:
        return None
    phase = day.toordinal() % 3
    if phase == 0:
        return "cesme"
    if phase == 2:
        return "uzunkuyu"
    return None


def _overlap_bounds():
    cesme_bbox = satellite.REGIONS["cesme"]["bbox"]
    uzunkuyu_bbox = satellite.REGIONS["uzunkuyu"]["bbox"]
    west = max(float(cesme_bbox[0]), float(uzunkuyu_bbox[0]))
    south = max(float(cesme_bbox[1]), float(uzunkuyu_bbox[1]))
    east = min(float(cesme_bbox[2]), float(uzunkuyu_bbox[2]))
    north = min(float(cesme_bbox[3]), float(uzunkuyu_bbox[3]))
    return west, south, east, north


def _is_overlap_example(item):
    if not isinstance(item, dict):
        return False
    try:
        latitude = float(item.get("enlem"))
        longitude = float(item.get("boylam"))
    except (TypeError, ValueError):
        return False
    west, south, east, north = _overlap_bounds()
    return west <= longitude <= east and south <= latitude <= north


def _overlap_only_audit(audit_payload, region_key):
    """Yalnız genişletilmiş, bilinen-kara devriye havuzunun ortak-şerit kısmını tut."""
    adjusted = copy.deepcopy(audit_payload)
    regions = adjusted.get("bolgeler") or {}
    region_data = regions.get(region_key) if isinstance(regions, dict) else None
    if not isinstance(region_data, dict):
        return {"bolgeler": {}}

    examples = region_data.get("kor_alan_devriye_ornekleri") or []
    overlap = [item for item in examples if _is_overlap_example(item)]
    region_data["kor_alan_devriye_ornekleri"] = overlap
    # Geniş diagnostik ``ornekler`` listesine düşme: dönüşüm yalnız 48 noktalık,
    # 500 m seyreltilmiş güvenli insan-devriye havuzundan beslensin.
    region_data["ornekler"] = []
    adjusted["bolgeler"] = {region_key: region_data}
    return adjusted


def _baseline_patrol(audit_payload, report_payload):
    current = (report_payload or {}).get("kor_alan_saha_devriyesi") or []
    if isinstance(current, list):
        clean = [dict(item) for item in current if isinstance(item, dict)]
        if clean and len(clean) <= coverage.TOTAL_LIMIT:
            return clean
    return route.select_diverse_coverage_patrol(
        audit_payload,
        report_payload,
        limit=coverage.TOTAL_LIMIT,
    )


def _region_item(items, region_key):
    return next(
        (
            dict(item)
            for item in items
            if str(item.get("bolge_anahtari") or "") == region_key
        ),
        None,
    )


def _eligible_overlap_candidates(
    audit_payload,
    report_payload,
    target_region,
    retained,
    rotation_day=None,
):
    overlap_audit = _overlap_only_audit(audit_payload, target_region)
    blocked = route._neighborhoods_from_calibration(report_payload)
    overlap_audit = route._prefer_other_neighborhoods(overlap_audit, blocked)
    region_data = (overlap_audit.get("bolgeler") or {}).get(target_region) or {}
    pool = coverage._candidate_pool(target_region, region_data)
    if not pool:
        return []

    preferred = [item for item in pool if item["alan_m2"] <= coverage.TARGET_MAX_AREA_M2]
    fallback = [item for item in pool if item["alan_m2"] > coverage.TARGET_MAX_AREA_M2]

    def rotated(items):
        if not items:
            return []
        offset = coverage._rotation_offset(
            report_payload,
            len(items),
            rotation_day=rotation_day,
        )
        return items[offset:] + items[:offset]

    active_points = coverage._active_points(report_payload)
    retained_point = coverage._point(retained)
    retained_neighborhood = coverage._neighborhood_key(retained.get("mahalle"))
    eligible = []
    for item in rotated(preferred) + rotated(fallback):
        point = coverage._point(item)
        if point is None:
            continue
        if not coverage._far_enough(point, active_points, coverage.ACTIVE_DISTANCE_M):
            continue
        if retained_point is not None and not coverage._far_enough(
            point,
            [retained_point],
            coverage.CROSS_REGION_DISTANCE_M,
        ):
            continue
        eligible.append(dict(item))

    if not eligible:
        return []

    # Aynı mahalleyi iki devriye slotunda tekrar etme; fakat güvenli alternatif yoksa
    # bölgeyi bütünüyle kör bırakmamak için ilk uygun adayı yine koru.
    different = [
        item
        for item in eligible
        if coverage._neighborhood_key(item.get("mahalle")) != retained_neighborhood
    ]
    return different or eligible


def select_overlap_rotated_patrol(
    audit_payload,
    report_payload,
    baseline=None,
    rotation_day=None,
):
    baseline = list(baseline or _baseline_patrol(audit_payload, report_payload))
    if len(baseline) < 2:
        return baseline

    target = _rotation_target(report_payload, rotation_day=rotation_day)
    if target not in ROTATION_REGIONS:
        return baseline
    retained_region = "uzunkuyu" if target == "cesme" else "cesme"
    target_current = _region_item(baseline, target)
    retained = _region_item(baseline, retained_region)
    if target_current is None or retained is None:
        return baseline

    # Dönüşüm yalnız gerçekten bir çekirdeği koruyabiliyorsak çalışsın. Baseline zaten
    # örtüşmeye düşmüşse ikinci slotu da örtüşmeye çekerek çekirdeği tamamen kaybetme.
    if not route._is_region_core_example(retained_region, retained):
        return baseline

    candidates = _eligible_overlap_candidates(
        audit_payload,
        report_payload,
        target,
        retained,
        rotation_day=rotation_day,
    )
    if not candidates:
        return baseline

    replacement = candidates[0]
    updated = []
    for item in baseline:
        region_key = str(item.get("bolge_anahtari") or "")
        updated.append(dict(replacement) if region_key == target else dict(item))

    assert len(updated) == len(baseline), "Örtüşme dönüşümü devriye sayısını değiştirdi."
    assert sum(_is_overlap_example(item) for item in updated) >= 1
    assert route._is_region_core_example(retained_region, _region_item(updated, retained_region))
    return updated


def _markdown(items):
    section = coverage._markdown(items)
    old = "Aynı görüntü günlerce değişmezse noktalar günlük rotasyonla değişir."
    new = (
        old
        + " Çeşme-Uzunkuyu ortak Sentinel şeridi çekirdek tercihi nedeniyle kalıcı "
        "biçimde aç kalmasın diye üç günlük dönüşüm uygulanır; güvenli aday yoksa "
        "mevcut çekirdek seçimi korunur."
    )
    return section.replace(old, new, 1)


def update_overlap_rotation():
    if not AUDIT_JSON.exists() or not REPORT_JSON.exists():
        return []

    audit = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
    report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    rotation_day = coverage._rotation_day(report)
    baseline = _baseline_patrol(audit, report)
    selected = select_overlap_rotated_patrol(
        audit,
        report,
        baseline=baseline,
        rotation_day=rotation_day,
    )

    report["kor_alan_saha_devriyesi"] = selected
    report["kor_alan_saha_devriyesi_rotasyon_tarihi"] = rotation_day.isoformat()
    report["kor_alan_saha_devriyesi_notu"] = NOTE
    REPORT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if FIELD_REPORT_MD.exists():
        current = FIELD_REPORT_MD.read_text(encoding="utf-8")
        FIELD_REPORT_MD.write_text(
            coverage._inject(current, _markdown(selected)),
            encoding="utf-8",
        )
    return selected


def _self_check():
    coverage._self_check()

    audit = {
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "bolge": "Çeşme",
                "kara_referans_sahne_sayisi": 8,
                "kor_alan_devriye_ornekleri": [
                    {"mahalle_yaklasik": "Ovacık", "enlem": 38.25, "boylam": 26.35, "alan_m2": 400, "neden": "BULUT_GOLGE_KALICI"},
                    {"mahalle_yaklasik": "Alaçatı", "enlem": 38.24, "boylam": 26.48, "alan_m2": 500, "neden": "BULUT_GOLGE_KALICI"},
                ],
            },
            "uzunkuyu": {
                "durum": "ok",
                "bolge": "Uzunkuyu",
                "kara_referans_sahne_sayisi": 8,
                "kor_alan_devriye_ornekleri": [
                    {"mahalle_yaklasik": "Uzunkuyu", "enlem": 38.20, "boylam": 26.58, "alan_m2": 400, "neden": "BULUT_GOLGE_KALICI"},
                    {"mahalle_yaklasik": "Germiyan", "enlem": 38.32, "boylam": 26.50, "alan_m2": 600, "neden": "BULUT_GOLGE_KALICI"},
                ],
            },
        }
    }

    def report_for(raw_date):
        return {
            "rapor_tarihi": raw_date,
            "saha_adaylari": [],
            "kuru_zemin_kalibrasyon_kontrolu": [],
        }

    # 01.09.2026: Çeşme slotu ortak şeride döner, Uzunkuyu çekirdeği korunur.
    first_report = report_for("2026-09-01")
    first_baseline = route.select_diverse_coverage_patrol(audit, first_report)
    first = select_overlap_rotated_patrol(audit, first_report, first_baseline)
    assert len(first) == 2, first
    assert _is_overlap_example(_region_item(first, "cesme")), first
    assert route._is_region_core_example("uzunkuyu", _region_item(first, "uzunkuyu")), first

    # Ertesi gün iki çekirdek aynen korunur.
    second_report = report_for("2026-09-02")
    second_baseline = route.select_diverse_coverage_patrol(audit, second_report)
    second = select_overlap_rotated_patrol(audit, second_report, second_baseline)
    assert second == second_baseline, (second, second_baseline)

    # Üçüncü gün Uzunkuyu slotu ortak şeride döner, Çeşme çekirdeği korunur.
    third_report = report_for("2026-09-03")
    third_baseline = route.select_diverse_coverage_patrol(audit, third_report)
    third = select_overlap_rotated_patrol(audit, third_report, third_baseline)
    assert _is_overlap_example(_region_item(third, "uzunkuyu")), third
    assert route._is_region_core_example("cesme", _region_item(third, "cesme")), third

    # Bayat rapor 01.09'da kalsa bile 02.09 yerel gününde örtüşme fazı dünde kalmamalı.
    stale_day = date(2026, 9, 2)
    assert _rotation_target(first_report, rotation_day=stale_day) is None
    stale = select_overlap_rotated_patrol(
        audit,
        first_report,
        first_baseline,
        rotation_day=stale_day,
    )
    assert stale == first_baseline, (stale, first_baseline)

    # Örtüşme havuzu yoksa mevcut iki çekirdek seçimi bozulmamalı.
    no_overlap = copy.deepcopy(audit)
    for region_data in no_overlap["bolgeler"].values():
        region_data["kor_alan_devriye_ornekleri"] = [
            item
            for item in region_data["kor_alan_devriye_ornekleri"]
            if not _is_overlap_example(item)
        ]
    base = route.select_diverse_coverage_patrol(no_overlap, first_report)
    assert select_overlap_rotated_patrol(no_overlap, first_report, base) == base

    # Örtüşme adayı aktif görevin 150 m yakınına düşerse dönüşüm reddedilir.
    blocked_report = report_for("2026-09-01")
    blocked_report["saha_adaylari"] = [
        {"saha_durumu": "KONTROLE_GIT", "enlem": 38.24, "boylam": 26.48}
    ]
    blocked_baseline = route.select_diverse_coverage_patrol(audit, blocked_report)
    blocked = select_overlap_rotated_patrol(audit, blocked_report, blocked_baseline)
    assert blocked == blocked_baseline, (blocked, blocked_baseline)


if __name__ == "__main__":
    _self_check()
    chosen = update_overlap_rotation()
    print(
        "Kör alan örtüşme dönüşümü güncellendi: "
        + (", ".join(str(item.get("mahalle") or "?") for item in chosen) or "ek nokta yok")
    )
