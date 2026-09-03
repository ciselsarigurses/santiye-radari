"""Aktif saha görevlerinden günün ilk üç kontrolünü raporda görünür kılar.

Bu katman yeni alarm üretmez, görev açmaz/kapatmaz ve Sentinel eşiklerini değiştirmez.
Tam saha listesi aynen korunur. Kısa liste ise erken hafriyat hedefine göre operasyonel
olarak yeniden sıralanır: insan tarafından istenen tekrar kontrol ve taze ERKEN sinyali
en üstte kalır; taze 800–2.000 m² PARSEL adayı gecikmiş eski kuyruğun önüne geçer.
GECİKEN görevlerde 250–800 m² küçük-güçlü adaylar, sonra 800–2.000 m² parsel ölçeği,
ardından daha geniş yüzey hareketleri gelir. Böylece saha backlog'u küçük kazı/temel
sinyalini ilk üçten düşürmez.

İki uydu bölgesi de aktifse kapsama dengesi korunmaya çalışılır; ancak daha zayıf
bir bölge adayı daha yüksek öncelikli taze adayı sırf coğrafi denge için listeden
çıkaramaz.

Ayrıca üretim maskesinin dışında kalan kuru-zemin diagnostiklerinden, yalnız kompakt,
izole ve lineer olmayan adaylardan uydu bölgesi başına en fazla bir, toplam iki
``alarm değil`` kalibrasyon noktası gösterir. Bu noktalar aktif görevlerin en az 120 m
dışında olmalıdır.
"""

from __future__ import annotations

import json
import math
from pathlib import Path


REPORT_JSON = Path(__file__).with_name("latest_report.json")
FIELD_REPORT_MD = Path(__file__).with_name("SAHA_RAPORU.md")
DRY_GROUND_AUDIT = Path(__file__).with_name("dry_ground_gap_audit.json")
SHORTLIST_LIMIT = 3
CALIBRATION_LIMIT = 2
CALIBRATION_PER_REGION_LIMIT = 1
CALIBRATION_MIN_DISTANCE_METERS = 120
ACTIVE_STATUSES = {"KONTROLE_GIT", "TEKRAR_GIT"}
# satellite.REGIONS["uzunkuyu"]["label"] ile aynı tutulmalıdır. Gülbahçe ayrı bir
# alarm bölgesi değildir; doğu Sentinel kapsamasının operasyonel parçasıdır.
SATELLITE_REGION_LABELS = (
    "Çeşme merkez · Alaçatı · Ilıca",
    "Uzunkuyu · Germiyan · Ildır · Gülbahçe",
)
ROUTE_PRIORITY = {
    "TEKRAR": 0,
    "ERKEN": 1,
    "PARSEL": 2,
    "GECİKEN": 3,
    "YÜKSEK": 4,
    "ORTA": 5,
    "BEKLEYEN": 6,
    "NORMAL": 7,
}
SATELLITE_PRIORITY = {"YÜKSEK": 0, "ORTA": 1, "NORMAL": 2}
SECTION_TITLE = "## Günün ilk 3 kontrolü"
CALIBRATION_SECTION_TITLE = "## Ek kuru zemin kalibrasyon kontrolü"
NEXT_SECTION = "## Bugün sahada kontrol edilecek uydu adayları"


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


def _actionable_candidates(candidates):
    eligible = []
    seen = set()
    for raw in candidates or []:
        if not isinstance(raw, dict):
            continue
        task_id = str(raw.get("gorev_id") or "").strip()
        status = str(raw.get("saha_durumu") or "KONTROLE_GIT").strip().upper()
        if not task_id or task_id in seen or status not in ACTIVE_STATUSES:
            continue
        try:
            latitude = float(raw.get("enlem"))
            longitude = float(raw.get("boylam"))
        except (TypeError, ValueError):
            continue
        item = dict(raw)
        item["enlem"] = round(latitude, 6)
        item["boylam"] = round(longitude, 6)
        eligible.append(item)
        seen.add(task_id)
    return eligible


def _route_priority_value(item):
    return ROUTE_PRIORITY.get(str(item.get("oncelik") or "").strip().upper(), 8)


def _overdue_excavation_class(item):
    """Gecikmiş backlog içinde erken şantiye ihtimaline yakın ölçeği öncele."""
    if str(item.get("oncelik") or "").strip().upper() != "GECİKEN":
        return 0
    area = max(_number(item.get("alan_m2"), 0), 0)
    size_class = str(item.get("boyut_sinifi") or "").strip().upper()
    signal = str(item.get("sinyal") or "").casefold()
    if 250 <= area <= 800 and (
        size_class == "KUCUK" or "küçük, güçlü" in signal
    ):
        return 0
    if 800 <= area < 2000:
        return 1
    if 2000 <= area < 10000:
        return 2
    if area >= 10000:
        return 3
    return 4


def _route_sort_key(item, original_index):
    priority = _route_priority_value(item)
    is_overdue = str(item.get("oncelik") or "").strip().upper() == "GECİKEN"
    if not is_overdue:
        return (priority, 0, 0.0, 0, 0, original_index)

    area = max(_number(item.get("alan_m2"), 0), 0)
    satellite_priority = SATELLITE_PRIORITY.get(
        str(item.get("uydu_onceligi") or "").strip().upper(), 3
    )
    waiting_days = max(int(_number(item.get("bekleme_gun"), 0)), 0)
    return (
        priority,
        _overdue_excavation_class(item),
        area,
        satellite_priority,
        -waiting_days,
        original_index,
    )


def _rank_for_route(eligible):
    indexed = list(enumerate(eligible))
    indexed.sort(key=lambda pair: _route_sort_key(pair[1], pair[0]))
    return [item for _, item in indexed]


def _balance_satellite_regions(eligible, selected, limit):
    """Aynı/üst kalite seviyesinde mümkünse iki uydu bölgesinden temsil bırak."""
    if limit < 2 or len(selected) < 2:
        return selected

    available_regions = [
        region
        for region in SATELLITE_REGION_LABELS
        if any(str(item.get("bolge") or "") == region for item in eligible)
    ]
    if len(available_regions) <= 1 or limit < len(available_regions):
        return selected

    selected_ids = {str(item.get("gorev_id") or "") for item in selected}
    region_counts = {
        region: sum(str(item.get("bolge") or "") == region for item in selected)
        for region in available_regions
    }

    for missing_region in available_regions:
        if region_counts.get(missing_region, 0) > 0:
            continue
        candidate = next(
            (
                item
                for item in eligible
                if str(item.get("bolge") or "") == missing_region
                and str(item.get("gorev_id") or "") not in selected_ids
            ),
            None,
        )
        if candidate is None:
            continue

        replace_index = None
        candidate_priority = _route_priority_value(candidate)
        for index in range(len(selected) - 1, -1, -1):
            region = str(selected[index].get("bolge") or "")
            if (
                region in region_counts
                and region_counts.get(region, 0) > 1
                and candidate_priority <= _route_priority_value(selected[index])
            ):
                replace_index = index
                break
        if replace_index is None:
            continue

        removed = selected[replace_index]
        removed_region = str(removed.get("bolge") or "")
        selected_ids.discard(str(removed.get("gorev_id") or ""))
        selected[replace_index] = candidate
        selected_ids.add(str(candidate.get("gorev_id") or ""))
        region_counts[removed_region] = max(region_counts.get(removed_region, 0) - 1, 0)
        region_counts[missing_region] = region_counts.get(missing_region, 0) + 1

    return selected


def select_shortlist(candidates, limit=SHORTLIST_LIMIT):
    """Taze/erken saha değerini öne al; alarm sayısını ve ana rapor sırasını değiştirme."""
    cap = max(int(limit), 0)
    if cap <= 0:
        return []

    eligible = _rank_for_route(_actionable_candidates(candidates))
    selected = [dict(item) for item in eligible[:cap]]
    selected = _balance_satellite_regions(eligible, selected, cap)
    for index, item in enumerate(selected, start=1):
        item["gunluk_sira"] = index
    return selected


def _load_dry_ground_audit():
    if not DRY_GROUND_AUDIT.exists():
        return {}
    try:
        payload = json.loads(DRY_GROUND_AUDIT.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _far_from_active(candidate, active_candidates, minimum_m=CALIBRATION_MIN_DISTANCE_METERS):
    point = _point(candidate)
    if point is None:
        return False
    for active in active_candidates or []:
        active_point = _point(active)
        if active_point is None:
            continue
        if _distance_m(point, active_point) < minimum_m:
            return False
    return True


def _far_from_selected(candidate, selected, minimum_m=CALIBRATION_MIN_DISTANCE_METERS):
    point = _point(candidate)
    if point is None:
        return False
    for old in selected or []:
        old_point = _point(old)
        if old_point is None:
            continue
        if _distance_m(point, old_point) < minimum_m:
            return False
    return True


def select_dry_ground_calibration(
    audit_payload,
    active_candidates,
    limit=CALIBRATION_LIMIT,
):
    """Alarm dışı diagnostikten iki bölgeyi dengeli saha kalibrasyonuna çevir."""
    cap = max(int(limit), 0)
    if cap <= 0 or not isinstance(audit_payload, dict):
        return []

    pool = []
    regions = audit_payload.get("bolgeler") or {}
    if not isinstance(regions, dict):
        return []

    for region_key, region_data in regions.items():
        if not isinstance(region_data, dict) or region_data.get("durum") != "ok":
            continue
        examples = region_data.get("saha_benzeri_ornekler") or []
        for raw in examples:
            if not isinstance(raw, dict):
                continue
            area = _number(raw.get("alan_m2"), 0)
            if not (250 <= area <= 2000):
                continue
            if not bool(raw.get("saha_benzeri_geometri")):
                continue
            if not bool(raw.get("izole_saha_benzeri")):
                continue
            if bool(raw.get("lineer_geometri_riski")):
                continue
            if not _far_from_active(raw, active_candidates):
                continue

            item = dict(raw)
            item["bolge_anahtari"] = str(region_key)
            item["bolge"] = str(region_data.get("bolge") or region_key)
            item["onceki_tarih"] = str(region_data.get("onceki_tarih") or "")
            item["son_tarih"] = str(region_data.get("son_tarih") or "")
            point = _point(item)
            if point is None:
                continue
            item["enlem"] = round(point[0], 6)
            item["boylam"] = round(point[1], 6)
            item["harita"] = (
                "https://www.google.com/maps/dir/?api=1&destination="
                f"{item['enlem']:.6f},{item['boylam']:.6f}"
            )
            item["kalibrasyon_durumu"] = "ALARM_DEGIL"
            pool.append(item)

    ranked = sorted(
        pool,
        key=lambda item: (
            -abs(_number(item.get("ortalama_bsi_degisim"), 0)),
            -_number(item.get("ortalama_rgb_farki"), 0),
            -_number(item.get("kompaktlik"), 0),
            _number(item.get("alan_m2"), 0),
            str(item.get("mahalle") or ""),
        ),
    )

    selected = []
    region_counts = {}

    if cap >= 2:
        for item in ranked:
            region_key = str(item.get("bolge_anahtari") or "")
            if not region_key:
                continue
            if region_counts.get(region_key, 0) >= CALIBRATION_PER_REGION_LIMIT:
                continue
            if not _far_from_selected(item, selected):
                continue
            selected.append(dict(item))
            region_counts[region_key] = region_counts.get(region_key, 0) + 1
            if len(selected) >= cap:
                return selected

    for item in ranked:
        if not _far_from_selected(item, selected):
            continue
        region_key = str(item.get("bolge_anahtari") or "")
        if (
            cap >= 2
            and region_key
            and region_counts.get(region_key, 0) >= CALIBRATION_PER_REGION_LIMIT
        ):
            continue
        selected.append(dict(item))
        if region_key:
            region_counts[region_key] = region_counts.get(region_key, 0) + 1
        if len(selected) >= cap:
            break
    return selected


def _shortlist_markdown(shortlist):
    lines = [
        SECTION_TITLE,
        "",
        "> Bu bölüm yeni alarm üretmez. Taze ERKEN/PARSEL sinyalini gecikmiş backlog'un önünde tutar; gecikenlerde küçük-güçlü ve parsel ölçeğini geniş yüzey hareketlerinden önce kontrol ettirir. İki uydu bölgesi dengesi yalnız daha yüksek öncelikli adayı düşürmeden uygulanır.",
        "",
    ]
    if not shortlist:
        lines.extend(["Bugün için eyleme dönük aktif uydu görevi yok.", ""])
        return "\n".join(lines)

    for item in shortlist:
        order = int(item.get("gunluk_sira") or 0)
        priority = str(item.get("oncelik") or "KONTROL")
        neighborhood = str(item.get("mahalle") or "Konum araştırılıyor")
        area = max(_number(item.get("alan_m2"), 0), 0)
        area_text = f" · yaklaşık {int(area):,} m²".replace(",", ".") if area else ""
        task_id = str(item.get("gorev_id") or "-")
        route = str(item.get("harita") or "").strip()
        route_text = (
            f" · [Yol tarifi]({route})"
            if route.startswith(("http://", "https://"))
            else ""
        )
        lines.append(
            f"{order}. **{priority} — {neighborhood}**{area_text} · Görev `{task_id}`{route_text}"
        )
    lines.append("")
    return "\n".join(lines)


def _calibration_markdown(calibration):
    lines = [
        CALIBRATION_SECTION_TITLE,
        "",
        "> **Alarm veya görev değildir.** Mevcut Sentinel üretim maskesinin dışında kalan kuru-zemin diagnostiklerinden, aktif radar görevlerinden en az 120 m uzakta olan ve uydu bölgesi başına en fazla bir tane seçilen güçlü örneklerdir. Toplam en fazla iki nokta gösterilir; amaç sahada bakıp gerçek hafriyat mı yanlış pozitif mi olduğunu öğrenerek algoritmayı iki bölgede de kalibre etmektir.",
        "",
    ]
    if not calibration:
        lines.extend(["Bugün için güvenli ek kuru-zemin kalibrasyon noktası seçilmedi.", ""])
        return "\n".join(lines)

    for index, item in enumerate(calibration, start=1):
        neighborhood = str(item.get("mahalle") or "Yakın bölge")
        area = int(max(_number(item.get("alan_m2"), 0), 0))
        area_text = f"{area:,}".replace(",", ".")
        route = str(item.get("harita") or "")
        bsi = abs(_number(item.get("ortalama_bsi_degisim"), 0))
        rgb = _number(item.get("ortalama_rgb_farki"), 0)
        start = str(item.get("onceki_tarih") or "?")
        end = str(item.get("son_tarih") or "?")
        lines.append(
            f"{index}. **KALİBRASYON — {neighborhood}** · yaklaşık {area_text} m² · "
            f"{start} → {end} · BSI Δ {bsi:.3f} · RGB Δ {rgb:.3f} · "
            f"[Yol tarifi]({route})"
        )
        lines.append(
            "   - Saha notu: Kazı/temel/şantiye varsa fotoğraf ve kısa not al; tarla sürümü, yol, bahçe temizliği veya başka bir neden ise onu yaz."
        )
    lines.append("")
    return "\n".join(lines)


def _inject_markdown(text, section):
    """Kısa liste bölümünü idempotent biçimde ana saha listesinden önce yerleştir."""
    text = str(text or "")
    start = text.find(SECTION_TITLE)
    if start >= 0:
        end = text.find(NEXT_SECTION, start)
        if end >= 0:
            text = text[:start].rstrip() + "\n\n" + text[end:]
        else:
            text = text[:start].rstrip() + "\n"
    marker_index = text.find(NEXT_SECTION)
    if marker_index < 0:
        return text.rstrip() + "\n\n" + section.rstrip() + "\n"
    prefix = text[:marker_index].rstrip()
    suffix = text[marker_index:].lstrip()
    return prefix + "\n\n" + section.rstrip() + "\n\n" + suffix


def _inject_calibration_markdown(text, section):
    """Kalibrasyon bölümünü kısa liste ile ana aday listesi arasına tek kez koy."""
    text = str(text or "")
    start = text.find(CALIBRATION_SECTION_TITLE)
    if start >= 0:
        end = text.find(NEXT_SECTION, start)
        if end >= 0:
            text = text[:start].rstrip() + "\n\n" + text[end:]
        else:
            text = text[:start].rstrip() + "\n"
    marker_index = text.find(NEXT_SECTION)
    if marker_index < 0:
        return text.rstrip() + "\n\n" + section.rstrip() + "\n"
    prefix = text[:marker_index].rstrip()
    suffix = text[marker_index:].lstrip()
    return prefix + "\n\n" + section.rstrip() + "\n\n" + suffix


def update_daily_shortlist():
    payload = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    candidates = payload.get("saha_adaylari", [])
    shortlist = select_shortlist(candidates)
    calibration = select_dry_ground_calibration(
        _load_dry_ground_audit(),
        _actionable_candidates(candidates),
    )
    payload["gunun_ilk_3_kontrolu"] = shortlist
    payload["gunun_ilk_3_notu"] = (
        "Yeni alarm üretmez; taze ERKEN/PARSEL sinyalini gecikmiş backlog'un önünde "
        "tutar. Gecikenlerde küçük-güçlü ve 800–2.000 m² parsel ölçeğini geniş yüzey "
        "hareketlerinden önce kontrol ettirir; bölge dengesi daha yüksek öncelikli "
        "adayı düşürmez."
    )
    payload["kuru_zemin_kalibrasyon_kontrolu"] = calibration
    payload["kuru_zemin_kalibrasyon_notu"] = (
        "Alarm/görev değildir; üretim maskesinin dışında kalan izole, saha-benzeri "
        "kuru-zemin değişimlerinden aktif görevlerin en az 120 m dışında uydu bölgesi "
        "başına en fazla bir, toplam iki örnek seçilir."
    )
    REPORT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if FIELD_REPORT_MD.exists():
        current = FIELD_REPORT_MD.read_text(encoding="utf-8")
        updated = _inject_markdown(current, _shortlist_markdown(shortlist))
        updated = _inject_calibration_markdown(
            updated,
            _calibration_markdown(calibration),
        )
        FIELD_REPORT_MD.write_text(updated, encoding="utf-8")
    return shortlist


def _self_check():
    west = SATELLITE_REGION_LABELS[0]
    east = SATELLITE_REGION_LABELS[1]
    assert east == "Uzunkuyu · Germiyan · Ildır · Gülbahçe", (
        "Gülbahçe doğu Sentinel rota bölgesi etiketinden düştü."
    )
    sample = [
        {
            "gorev_id": "U1", "saha_durumu": "KONTROLE_GIT", "oncelik": "ERKEN",
            "mahalle": "Alaçatı", "enlem": 38.2, "boylam": 26.4, "alan_m2": 600,
            "bolge": west,
        },
        {
            "gorev_id": "U2", "saha_durumu": "KONTROL_EDILDI", "oncelik": "ERKEN",
            "mahalle": "Ovacık", "enlem": 38.3, "boylam": 26.3, "alan_m2": 500,
            "bolge": west,
        },
        {
            "gorev_id": "U3", "saha_durumu": "TEKRAR_GIT", "oncelik": "TEKRAR",
            "mahalle": "Şifne", "enlem": 38.34, "boylam": 26.39, "alan_m2": 400,
            "bolge": west,
        },
        {
            "gorev_id": "U4", "saha_durumu": "KONTROLE_GIT", "oncelik": "PARSEL",
            "mahalle": "Ilıca", "enlem": 38.31, "boylam": 26.36, "alan_m2": 900,
            "bolge": west,
        },
        {
            "gorev_id": "U5", "saha_durumu": "KONTROLE_GIT", "oncelik": "ERKEN",
            "mahalle": "Ildır", "enlem": 38.42, "boylam": 26.57, "alan_m2": 400,
            "bolge": east,
        },
    ]
    chosen = select_shortlist(sample)
    assert [item["gorev_id"] for item in chosen] == ["U3", "U1", "U5"]
    assert [item["gunluk_sira"] for item in chosen] == [1, 2, 3]
    assert {item["bolge"] for item in chosen} == {west, east}

    one_region = select_shortlist(sample[:4])
    assert [item["gorev_id"] for item in one_region] == ["U3", "U1", "U4"]

    short_limit = select_shortlist(sample, limit=1)
    assert [item["gorev_id"] for item in short_limit] == ["U3"]

    backlog = [
        {
            "gorev_id": "OLD_BIG", "saha_durumu": "KONTROLE_GIT", "oncelik": "GECİKEN",
            "mahalle": "Musalla", "enlem": 38.30, "boylam": 26.30, "alan_m2": 4800,
            "bolge": west, "bekleme_gun": 4, "uydu_onceligi": "ORTA",
            "boyut_sinifi": "STANDART", "sinyal": "Bitişik yüzey/toprak değişimi adayı",
        },
        {
            "gorev_id": "OLD_SMALL", "saha_durumu": "KONTROLE_GIT", "oncelik": "GECİKEN",
            "mahalle": "Musalla", "enlem": 38.31, "boylam": 26.31, "alan_m2": 400,
            "bolge": west, "bekleme_gun": 3, "uydu_onceligi": "ORTA",
            "boyut_sinifi": "KUCUK", "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
        },
        {
            "gorev_id": "OLD_PARCEL", "saha_durumu": "KONTROLE_GIT", "oncelik": "GECİKEN",
            "mahalle": "Alaçatı", "enlem": 38.29, "boylam": 26.38, "alan_m2": 1200,
            "bolge": west, "bekleme_gun": 3, "uydu_onceligi": "NORMAL",
            "boyut_sinifi": "STANDART", "sinyal": "Bitişik yüzey/toprak değişimi adayı",
        },
        {
            "gorev_id": "EAST_PARCEL", "saha_durumu": "KONTROLE_GIT", "oncelik": "GECİKEN",
            "mahalle": "Uzunkuyu", "enlem": 38.33, "boylam": 26.65, "alan_m2": 1000,
            "bolge": east, "bekleme_gun": 3, "uydu_onceligi": "NORMAL",
            "boyut_sinifi": "STANDART", "sinyal": "Bitişik yüzey/toprak değişimi adayı",
        },
    ]
    focused = select_shortlist(backlog)
    assert focused[0]["gorev_id"] == "OLD_SMALL"
    assert "OLD_BIG" not in {item["gorev_id"] for item in focused}
    assert {item["bolge"] for item in focused} == {west, east}

    fresh_over_backlog = select_shortlist(
        [
            backlog[0],
            {
                "gorev_id": "FRESH_PARCEL", "saha_durumu": "KONTROLE_GIT",
                "oncelik": "PARSEL", "mahalle": "Ilıca", "enlem": 38.32,
                "boylam": 26.36, "alan_m2": 900, "bolge": west,
            },
            backlog[3],
        ],
        limit=2,
    )
    assert fresh_over_backlog[0]["gorev_id"] == "FRESH_PARCEL"

    no_weak_balance = select_shortlist(
        [
            {
                "gorev_id": "W1", "saha_durumu": "KONTROLE_GIT", "oncelik": "ERKEN",
                "mahalle": "Alaçatı", "enlem": 38.28, "boylam": 26.38, "alan_m2": 500,
                "bolge": west,
            },
            {
                "gorev_id": "W2", "saha_durumu": "KONTROLE_GIT", "oncelik": "ERKEN",
                "mahalle": "Ilıca", "enlem": 38.30, "boylam": 26.35, "alan_m2": 600,
                "bolge": west,
            },
            {
                "gorev_id": "E1", "saha_durumu": "KONTROLE_GIT", "oncelik": "GECİKEN",
                "mahalle": "Uzunkuyu", "enlem": 38.33, "boylam": 26.65, "alan_m2": 1000,
                "bolge": east,
            },
        ],
        limit=2,
    )
    assert [item["gorev_id"] for item in no_weak_balance] == ["W1", "W2"]

    audit = {
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "bolge": west,
                "onceki_tarih": "26.08.2026",
                "son_tarih": "29.08.2026",
                "saha_benzeri_ornekler": [
                    {
                        "mahalle": "Dalyan", "enlem": 38.3555, "boylam": 26.3002,
                        "alan_m2": 300, "ortalama_bsi_degisim": 0.32,
                        "ortalama_rgb_farki": 0.14, "kompaktlik": 0.59,
                        "saha_benzeri_geometri": True, "izole_saha_benzeri": True,
                        "lineer_geometri_riski": False,
                    },
                    {
                        "mahalle": "Yakın", "enlem": 38.2002, "boylam": 26.4000,
                        "alan_m2": 400, "ortalama_bsi_degisim": 0.50,
                        "ortalama_rgb_farki": 0.16, "kompaktlik": 0.60,
                        "saha_benzeri_geometri": True, "izole_saha_benzeri": True,
                        "lineer_geometri_riski": False,
                    },
                    {
                        "mahalle": "Lineer", "enlem": 38.36, "boylam": 26.31,
                        "alan_m2": 500, "ortalama_bsi_degisim": 0.60,
                        "ortalama_rgb_farki": 0.18, "kompaktlik": 0.10,
                        "saha_benzeri_geometri": True, "izole_saha_benzeri": True,
                        "lineer_geometri_riski": True,
                    },
                ],
            },
            "uzunkuyu": {
                "durum": "ok",
                "bolge": east,
                "onceki_tarih": "26.08.2026",
                "son_tarih": "29.08.2026",
                "saha_benzeri_ornekler": [
                    {
                        "mahalle": "Ildır", "enlem": 38.4009, "boylam": 26.6215,
                        "alan_m2": 300, "ortalama_bsi_degisim": 0.26,
                        "ortalama_rgb_farki": 0.16, "kompaktlik": 0.59,
                        "saha_benzeri_geometri": True, "izole_saha_benzeri": True,
                        "lineer_geometri_riski": False,
                    }
                ],
            },
        }
    }
    calibration = select_dry_ground_calibration(audit, _actionable_candidates(sample))
    assert len(calibration) == 2
    assert calibration[0]["mahalle"] == "Dalyan"
    assert {item["bolge_anahtari"] for item in calibration} == {"cesme", "uzunkuyu"}
    assert all(item["kalibrasyon_durumu"] == "ALARM_DEGIL" for item in calibration)
    assert "destination=38.355500,26.300200" in calibration[0]["harita"]

    single_calibration = select_dry_ground_calibration(
        audit,
        _actionable_candidates(sample),
        limit=1,
    )
    assert len(single_calibration) == 1
    assert single_calibration[0]["mahalle"] == "Dalyan"

    rendered = _calibration_markdown(calibration)
    assert "destination=38.355500,26.300200" in rendered
    assert "Ildır" in rendered

    base = "# Rapor\n\n" + NEXT_SECTION + "\n\nAdaylar\n"
    once = _inject_markdown(base, _shortlist_markdown(chosen))
    once = _inject_calibration_markdown(once, rendered)
    twice = _inject_markdown(once, _shortlist_markdown(chosen))
    twice = _inject_calibration_markdown(twice, rendered)
    assert once == twice, "Kısa liste/kalibrasyon bölümü tekrar çalıştırmada çoğalmamalı."
    assert once.count(SECTION_TITLE) == 1
    assert once.count(CALIBRATION_SECTION_TITLE) == 1
    assert once.find(SECTION_TITLE) < once.find(CALIBRATION_SECTION_TITLE) < once.find(NEXT_SECTION)


if __name__ == "__main__":
    _self_check()
    shortlist = update_daily_shortlist()
    print(
        "Günün ilk kontrol kısa listesi güncellendi: "
        + (", ".join(str(item.get("gorev_id")) for item in shortlist) or "aktif görev yok")
    )
