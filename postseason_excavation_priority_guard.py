"""15 Eylül sonrası taze hafriyat/temel sinyallerini günlük saha rotasında öne alır.

Bu katman yalnız operasyon sırasını değiştirir. Yeni alarm/görev üretmez, 250 m² ana
Sentinel eşiğini değiştirmez ve 150–249 m² MİKRO ŞANTİYE diagnostiklerini saha
görevine yükseltmez. İnsan tarafından TEKRAR_GIT denmiş kayıtlar her zaman en
yüksek öncelikte kalır.

15 Eylül 2026 ve sonrasında, yeni Sentinel görüntüsünde görülen; tarihsel taşınmış
kanıt olmayan; 250 m² ve üzeri; ERKEN/PARSEL veya küçük-güçlü kompakt karakter
taşıyan adaylar eski/backlog adaylarının önüne alınır. Geniş yüzey hareketlerinin
yanlışlıkla öne çıkmasını önlemek için taze-kazı yükseltmesi 5.000 m² ile sınırlıdır.
Geniş-yüzey arka-plan katmanı tarafından operasyon listesinden ayrılan kayıtlar zaten
buraya gelmez; bu guard da 10.000 m² ve üstünü hiçbir koşulda yükseltmez.

Ayrıca daha eski bir Sentinel sahnesinde güçlü temporal + lokal/kompakt MİKRO izi
veya sahada doğrulanmış yıkım/parsel temizliği olan aynı küçük alan, sonraki yeni
sahnede bağımsız olarak 250 m²+ ana adaya dönüşürse bu geçmiş yalnız taze-kazı bandı
içinde sıralama kanıtı olarak kullanılır. MİKRO iz veya saha öncülü tek başına alarm
veya saha görevi üretmez.

15 Eylül öncesinde üretim raporuna dokunmaz; yalnız --self-check ile sezon açılışı
davranışı bugünden doğrulanabilir.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import daily_route_shortlist as route
import daily_route_freshness_guard as freshness


ISTANBUL = ZoneInfo("Europe/Istanbul")
BASE = Path(__file__).resolve().parent
MICRO_WATCHLIST_FILE = BASE / "micro_site_watchlist.json"
FIELD_DB_FILE = BASE / "santiye.db"
FULL_OPERATION_START = date(2026, 9, 15)
MAIN_ALARM_MIN_M2 = 250
MICRO_MIN_M2 = 150
MICRO_MAX_M2 = 249
MICRO_PRECURSOR_MAX_DISTANCE_M = 25.0
FIELD_PRECURSOR_OUTCOMES = {"YIKIM_TEMIZLIK"}
FIELD_PRECURSOR_MIN_M2 = 150
FIELD_PRECURSOR_MAX_M2 = 5_000
FIELD_PRECURSOR_MAX_DISTANCE_M = 25.0
FIELD_PRECURSOR_MAX_AGE_DAYS = 60
FRESH_EXCAVATION_MAX_M2 = 5_000
STRONG_SMALL_MAX_M2 = 800
ALLOWED_SATELLITE_PRIORITIES = {"YÜKSEK", "ORTA"}
FRESH_ROUTE_PRIORITIES = {"ERKEN", "PARSEL"}
NOTE = (
    "15 Eylül sonrası operasyon modu: yeni Sentinel görüntüsünde beliren 250 m²+ "
    "kompakt ERKEN/PARSEL ve küçük-güçlü hafriyat/temel sinyalleri eski backlog'un "
    "önüne alınır. Aynı küçük alanda daha eski güçlü 150–249 m² MİKRO izi veya "
    "sahada doğrulanmış yıkım/parsel temizliği varsa, bağımsız 250 m²+ taze aday "
    "kendi taze-kazı bandı içinde öne alınır. TEKRAR_GIT her zaman en yüksek "
    "önceliktedir; öncül kanıt tek başına saha görevine yükseltilmez."
)


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _local_day(value=None):
    if value is None:
        return datetime.now(ISTANBUL).date()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.date()
        return value.astimezone(ISTANBUL).date()
    if isinstance(value, date):
        return value
    raise TypeError("local_day date/datetime olmalı.")


def _parse_scene_date(value):
    text = str(value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _distance_m(lat1, lon1, lat2, lon2):
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _load_micro_watchlist():
    """Diagnostik yan-katman bozuksa ana rota güvenle mikro bonusu olmadan devam etsin."""
    try:
        payload = json.loads(MICRO_WATCHLIST_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("alarm") is True or payload.get("saha_gorevi") is True:
        return {}
    if int(_number(payload.get("ana_uretim_esigi_m2"), 0)) != MAIN_ALARM_MIN_M2:
        return {}
    interval = tuple(payload.get("mikro_aralik_m2") or ())
    if interval != (MICRO_MIN_M2, MICRO_MAX_M2):
        return {}
    return payload


def _field_precursors_from_connection(connection):
    """Sahada doğrulanmış fiziksel öncülleri salt-okunur sıralama kanıtına dönüştür."""
    try:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(saha_sonuclari)")
        }
    except sqlite3.Error:
        return []
    required = {"gorev_id", "sonuc", "enlem", "boylam", "alan_m2", "son_tarih"}
    if not required.issubset(columns):
        return []

    placeholders = ",".join("?" for _ in FIELD_PRECURSOR_OUTCOMES)
    try:
        rows = connection.execute(
            f"""SELECT gorev_id,sonuc,enlem,boylam,alan_m2,son_tarih
            FROM saha_sonuclari
            WHERE sonuc IN ({placeholders})
              AND enlem IS NOT NULL AND boylam IS NOT NULL
              AND alan_m2 IS NOT NULL AND son_tarih IS NOT NULL""",
            tuple(sorted(FIELD_PRECURSOR_OUTCOMES)),
        ).fetchall()
    except sqlite3.Error:
        return []

    precursors = []
    for task_id, outcome, latitude, longitude, area, last_date in rows:
        area_value = _number(area, None)
        scene_day = _parse_scene_date(last_date)
        if area_value is None or not (
            FIELD_PRECURSOR_MIN_M2 <= area_value <= FIELD_PRECURSOR_MAX_M2
        ):
            continue
        if scene_day is None:
            continue
        precursors.append(
            {
                "gorev_id": str(task_id or "").strip(),
                "sonuc": str(outcome or "").strip().upper(),
                "enlem": _number(latitude, None),
                "boylam": _number(longitude, None),
                "alan_m2": area_value,
                "son_tarih": scene_day.strftime("%d.%m.%Y"),
            }
        )
    return precursors


def _load_field_precursors():
    """DB okunamazsa ana rota saha-öncül bonusu olmadan güvenle devam etsin."""
    if not FIELD_DB_FILE.exists():
        return []
    connection = None
    try:
        connection = sqlite3.connect(
            f"file:{FIELD_DB_FILE}?mode=ro", uri=True, timeout=5
        )
        return _field_precursors_from_connection(connection)
    except sqlite3.Error:
        return []
    finally:
        if connection is not None:
            connection.close()


def _is_manual_repeat(item):
    status = str(item.get("saha_durumu") or "").strip().upper()
    priority = str(item.get("oncelik") or "").strip().upper()
    return status == "TEKRAR_GIT" or priority == "TEKRAR"


def _is_fresh_excavation_candidate(item):
    """Yalnız ölçülü, taze ve şantiye ölçeğine yakın ana-alarm adayını yükselt."""
    if not isinstance(item, dict) or _is_manual_repeat(item):
        return False

    # Mikro Şantiye 150–249 m² diagnostik kalır; ana rota yükseltmesi 250 m²'den başlar.
    area = max(_number(item.get("alan_m2"), 0.0), 0.0)
    if not (MAIN_ALARM_MIN_M2 <= area <= FRESH_EXCAVATION_MAX_M2):
        return False

    if item.get("yeni_goruntu") is not True:
        return False

    # Sezon açılışından önceki Sentinel sahnesi, workflow 15 Eylül sabahı
    # çalıştığında yeni_goruntu biti hâlâ açık olsa bile TAZE KAZI sayılamaz.
    # Operasyonel ağırlık yalnız 15 Eylül ve sonrasına ait gerçek sahne kanıtıyla başlar.
    evidence_day = _parse_scene_date(item.get("son_tarih"))
    if evidence_day is None or evidence_day < FULL_OPERATION_START:
        return False

    if freshness._historical_evidence_rank(item) != 0:
        return False

    # Ölçülmüş geniş/geometrik arka-plan işareti operasyon önceliği olamaz.
    if item.get("genis_geometri_riski") is True:
        return False
    if str(item.get("izleme") or "").strip().upper() == "ARKA_PLAN_GENIS_YUZEY":
        return False
    if item.get("alarm") is False or item.get("saha_gorevi") is False:
        return False

    priority = str(item.get("oncelik") or "").strip().upper()
    if priority in FRESH_ROUTE_PRIORITIES:
        return True

    size_class = str(item.get("boyut_sinifi") or "").strip().upper()
    satellite_priority = str(item.get("uydu_onceligi") or "").strip().upper()
    signal = str(item.get("sinyal") or "").casefold()
    strong_small = (
        area <= STRONG_SMALL_MAX_M2
        and (size_class == "KUCUK" or "küçük, güçlü" in signal)
        and satellite_priority in ALLOWED_SATELLITE_PRIORITIES
    )
    return strong_small


def _field_precursor_match(item, field_precursors):
    """Daha eski saha-teyitli yıkım/temizliğin taze 250+ adayla çakışmasını ölç."""
    if not _is_fresh_excavation_candidate(item):
        return None
    if not isinstance(field_precursors, (list, tuple)):
        return None

    current_date = _parse_scene_date(item.get("son_tarih"))
    latitude = _number(item.get("enlem"), None)
    longitude = _number(item.get("boylam"), None)
    if current_date is None or latitude is None or longitude is None:
        return None

    best = None
    best_distance = None
    for entry in field_precursors:
        if not isinstance(entry, dict):
            continue
        outcome = str(entry.get("sonuc") or "").strip().upper()
        if outcome not in FIELD_PRECURSOR_OUTCOMES:
            continue
        area = _number(entry.get("alan_m2"), None)
        if area is None or not (
            FIELD_PRECURSOR_MIN_M2 <= area <= FIELD_PRECURSOR_MAX_M2
        ):
            continue
        previous_date = _parse_scene_date(entry.get("son_tarih"))
        if previous_date is None or previous_date >= current_date:
            continue
        age_days = (current_date - previous_date).days
        if age_days > FIELD_PRECURSOR_MAX_AGE_DAYS:
            continue
        previous_lat = _number(entry.get("enlem"), None)
        previous_lon = _number(entry.get("boylam"), None)
        if previous_lat is None or previous_lon is None:
            continue
        distance = _distance_m(latitude, longitude, previous_lat, previous_lon)
        if distance > FIELD_PRECURSOR_MAX_DISTANCE_M:
            continue
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best = {
                "saha_oncul_eslesmesi": True,
                "saha_oncul_sonuc": outcome,
                "saha_oncul_gorev_id": str(entry.get("gorev_id") or ""),
                "saha_oncul_mesafe_m": round(distance, 1),
                "saha_oncul_son_tarih": previous_date.strftime("%d.%m.%Y"),
                "saha_oncul_kanit_notu": (
                    "Aynı küçük alanda daha eski sahada doğrulanmış yıkım/parsel "
                    "temizliği ile güncel bağımsız 250 m²+ taze Sentinel adayı 25 m "
                    "içinde örtüşüyor. Bu yalnız taze ana aday için sıralama kanıtıdır; "
                    "saha öncülü tek başına alarm veya yeni görev üretmez."
                ),
            }
    return best


def _micro_precursor_match(item, micro_watchlist):
    """Daha eski güçlü MİKRO izin, taze 250+ adayla aynı küçük alanda olup olmadığını ölç."""
    if not _is_fresh_excavation_candidate(item):
        return None
    if not isinstance(micro_watchlist, dict):
        return None

    current_date = _parse_scene_date(item.get("son_tarih"))
    latitude = _number(item.get("enlem"), None)
    longitude = _number(item.get("boylam"), None)
    if current_date is None or latitude is None or longitude is None:
        return None

    best = None
    best_distance = None
    for entry in micro_watchlist.get("adaylar") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("alarm") is True or entry.get("saha_gorevi") is True:
            continue
        area = _number(entry.get("alan_m2"), None)
        if area is None or not (MICRO_MIN_M2 <= area <= MICRO_MAX_M2):
            continue
        previous_date = _parse_scene_date(entry.get("son_guclu_gorulme_tarihi"))
        if previous_date is None or previous_date >= current_date:
            continue
        if not str(entry.get("son_guclu_sentinel_item") or "").strip():
            continue
        try:
            scene_count = int(entry.get("farkli_sentinel_sahnesi_gorulme_sayisi") or 0)
        except (TypeError, ValueError):
            scene_count = 0
        if scene_count < 1:
            continue

        first_lat = _number(entry.get("ilk_enlem"), _number(entry.get("enlem"), None))
        first_lon = _number(entry.get("ilk_boylam"), _number(entry.get("boylam"), None))
        if first_lat is None or first_lon is None:
            continue
        distance = _distance_m(latitude, longitude, first_lat, first_lon)
        if distance > MICRO_PRECURSOR_MAX_DISTANCE_M:
            continue
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best = {
                "mikro_oncul_iz_eslesmesi": True,
                "mikro_oncul_iz_id": str(entry.get("mikro_iz_id") or ""),
                "mikro_oncul_mesafe_m": round(distance, 1),
                "mikro_oncul_son_guclu_tarih": previous_date.strftime("%d.%m.%Y"),
                "mikro_oncul_son_guclu_sentinel_item": str(
                    entry.get("son_guclu_sentinel_item") or ""
                ),
                "mikro_oncul_kanit_notu": (
                    "Daha eski Sentinel sahnesindeki güçlü 150–249 m² temporal/lokal "
                    "MİKRO iz ile güncel bağımsız 250 m²+ aday 25 m içinde örtüşüyor. "
                    "Bu yalnız taze ana aday için sıralama kanıtıdır; MİKRO tek başına "
                    "alarm veya saha görevi değildir."
                ),
            }
    return best


def _priority_band(item):
    if _is_manual_repeat(item):
        return 0
    if _is_fresh_excavation_candidate(item):
        return 1
    return 2


def _sort_key(item, original_index, micro_watchlist=None, field_precursors=None):
    base = route._route_sort_key(item, original_index)
    band = _priority_band(item)
    field_rank = (
        0 if band == 1 and _field_precursor_match(item, field_precursors or []) else 1
    )
    micro_rank = 0 if band == 1 and _micro_precursor_match(item, micro_watchlist or {}) else 1
    return (band, field_rank, micro_rank, *base)


def _balance_regions(ranked, selected, limit):
    """Coğrafi dengeyi taze-kazı önceliğini bozmadan koru."""
    if limit < 2 or len(selected) < 2:
        return selected

    regions = [
        region
        for region in route.SATELLITE_REGION_LABELS
        if any(str(item.get("bolge") or "") == region for item in ranked)
    ]
    if len(regions) <= 1 or limit < len(regions):
        return selected

    selected_ids = {str(item.get("gorev_id") or "") for item in selected}
    region_counts = {
        region: sum(str(item.get("bolge") or "") == region for item in selected)
        for region in regions
    }

    for missing_region in regions:
        if region_counts.get(missing_region, 0) > 0:
            continue
        candidate = next(
            (
                item
                for item in ranked
                if str(item.get("bolge") or "") == missing_region
                and str(item.get("gorev_id") or "") not in selected_ids
            ),
            None,
        )
        if candidate is None:
            continue

        candidate_band = _priority_band(candidate)
        candidate_base_priority = route._route_priority_value(candidate)
        replace_index = None
        for index in range(len(selected) - 1, -1, -1):
            current = selected[index]
            current_region = str(current.get("bolge") or "")
            if current_region not in region_counts or region_counts.get(current_region, 0) <= 1:
                continue
            # Eski/backlog aday coğrafi denge uğruna taze hafriyat adayını düşüremez.
            if candidate_band > _priority_band(current):
                continue
            if candidate_band == _priority_band(current):
                if candidate_base_priority > route._route_priority_value(current):
                    continue
            replace_index = index
            break

        if replace_index is None:
            continue

        removed = selected[replace_index]
        removed_region = str(removed.get("bolge") or "")
        selected_ids.discard(str(removed.get("gorev_id") or ""))
        selected[replace_index] = dict(candidate)
        selected_ids.add(str(candidate.get("gorev_id") or ""))
        region_counts[removed_region] = max(region_counts.get(removed_region, 0) - 1, 0)
        region_counts[missing_region] = region_counts.get(missing_region, 0) + 1

    return selected


def _annotate_field_precursor(item, field_precursors):
    keys = (
        "saha_oncul_eslesmesi",
        "saha_oncul_sonuc",
        "saha_oncul_gorev_id",
        "saha_oncul_mesafe_m",
        "saha_oncul_son_tarih",
        "saha_oncul_kanit_notu",
    )
    for key in keys:
        item.pop(key, None)
    match = _field_precursor_match(item, field_precursors)
    if match:
        item.update(match)
    return item


def _annotate_micro_precursor(item, micro_watchlist):
    keys = (
        "mikro_oncul_iz_eslesmesi",
        "mikro_oncul_iz_id",
        "mikro_oncul_mesafe_m",
        "mikro_oncul_son_guclu_tarih",
        "mikro_oncul_son_guclu_sentinel_item",
        "mikro_oncul_kanit_notu",
    )
    for key in keys:
        item.pop(key, None)
    match = _micro_precursor_match(item, micro_watchlist)
    if match:
        item.update(match)
    return item


def select_postseason_shortlist(
    candidates,
    limit=route.SHORTLIST_LIMIT,
    local_day=None,
    micro_watchlist=None,
    field_precursors=None,
):
    cap = max(int(limit), 0)
    if cap <= 0:
        return []

    micro_watchlist = micro_watchlist if isinstance(micro_watchlist, dict) else {}
    field_precursors = field_precursors if isinstance(field_precursors, (list, tuple)) else []
    # Legacy Uzunkuyu etiketi ile Gülbahçe-dahil yeni etiketi aynı doğu bölgesi kabul et.
    eligible = freshness._normalized_actionable_candidates(candidates)
    indexed = list(enumerate(eligible))
    indexed.sort(
        key=lambda pair: _sort_key(
            pair[1], pair[0], micro_watchlist, field_precursors
        )
    )
    ranked = [item for _, item in indexed]

    selected = [dict(item) for item in ranked[:cap]]
    selected = _balance_regions(ranked, selected, cap)
    for index, item in enumerate(selected, start=1):
        _annotate_field_precursor(item, field_precursors)
        _annotate_micro_precursor(item, micro_watchlist)
        item["gunluk_sira"] = index
    return selected


def _shortlist_markdown(shortlist):
    lines = [route.SECTION_TITLE, "", f"> {NOTE}", ""]
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
        map_url = str(item.get("harita") or "").strip()
        route_text = (
            f" · [Yol tarifi]({map_url})"
            if map_url.startswith(("http://", "https://"))
            else ""
        )
        fresh_tag = " · **TAZE KAZI ÖNCELİĞİ**" if _is_fresh_excavation_candidate(item) else ""
        field_tag = " · **SAHA ÖNCÜLÜ→TAZE KAZI**" if item.get("saha_oncul_eslesmesi") else ""
        micro_tag = " · **MİKRO→ANA DEVAM KANITI**" if item.get("mikro_oncul_iz_eslesmesi") else ""
        lines.append(
            f"{order}. **{priority} — {neighborhood}**{area_text}{fresh_tag}{field_tag}{micro_tag} · "
            f"Görev `{task_id}`{route_text}"
        )
    lines.append("")
    return "\n".join(lines)


def apply_postseason_priority(local_day=None):
    day = _local_day(local_day)
    if day < FULL_OPERATION_START:
        return False, []

    payload = json.loads(route.REPORT_JSON.read_text(encoding="utf-8"))
    candidates = payload.get("saha_adaylari") or []
    micro_watchlist = _load_micro_watchlist()
    field_precursors = _load_field_precursors()
    shortlist = select_postseason_shortlist(
        candidates,
        local_day=day,
        micro_watchlist=micro_watchlist,
        field_precursors=field_precursors,
    )
    promoted = [
        str(item.get("gorev_id") or "")
        for item in shortlist
        if _is_fresh_excavation_candidate(item)
    ]
    field_precursor_promoted = [
        str(item.get("gorev_id") or "")
        for item in shortlist
        if item.get("saha_oncul_eslesmesi") is True
    ]
    micro_precursor_promoted = [
        str(item.get("gorev_id") or "")
        for item in shortlist
        if item.get("mikro_oncul_iz_eslesmesi") is True
    ]

    payload["gunun_ilk_3_kontrolu"] = shortlist
    payload["gunun_ilk_3_notu"] = NOTE
    payload["postseason_excavation_priority"] = {
        "aktif": True,
        "baslangic_tarihi": FULL_OPERATION_START.isoformat(),
        "ana_alarm_alt_esigi_m2": MAIN_ALARM_MIN_M2,
        "taze_kazi_oncelik_ust_siniri_m2": FRESH_EXCAVATION_MAX_M2,
        "mikro_santiye": "150-249 m² diagnostik; doğrudan saha görevine yükseltilmez",
        "mikro_oncul_esleme_mesafesi_m": MICRO_PRECURSOR_MAX_DISTANCE_M,
        "mikro_oncul_iz_eslesen_gorevler": micro_precursor_promoted,
        "saha_oncul_sonuclari": sorted(FIELD_PRECURSOR_OUTCOMES),
        "saha_oncul_esleme_mesafesi_m": FIELD_PRECURSOR_MAX_DISTANCE_M,
        "saha_oncul_azami_yas_gun": FIELD_PRECURSOR_MAX_AGE_DAYS,
        "saha_oncul_eslesen_gorevler": field_precursor_promoted,
        "one_alinan_gorevler": promoted,
        "not": (
            "Yalnız mevcut aktif görevlerin sırasını değiştirir; alarm/görev üretmez. "
            "Daha eski güçlü MİKRO izi veya sahada doğrulanmış yıkım/parsel temizliği "
            "yalnız zaten bağımsız 250 m²+ taze ana adayda sıralama kanıtıdır. "
            "TEKRAR_GIT en yüksek öncelikte kalır."
        ),
    }

    before_json = route.REPORT_JSON.read_text(encoding="utf-8")
    after_json = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    changed = before_json != after_json
    if changed:
        route.REPORT_JSON.write_text(after_json, encoding="utf-8")

    if route.FIELD_REPORT_MD.exists():
        current = route.FIELD_REPORT_MD.read_text(encoding="utf-8")
        updated = route._inject_markdown(current, _shortlist_markdown(shortlist))
        if updated != current:
            route.FIELD_REPORT_MD.write_text(updated, encoding="utf-8")
            changed = True

    return changed, shortlist


def _self_check():
    route._self_check()

    west = freshness.CANONICAL_WEST_REGION
    east = freshness.CANONICAL_EAST_REGION

    repeat = {
        "gorev_id": "REPEAT",
        "saha_durumu": "TEKRAR_GIT",
        "oncelik": "TEKRAR",
        "mahalle": "Alaçatı",
        "enlem": 38.285,
        "boylam": 26.375,
        "alan_m2": 900,
        "bolge": west,
        "yeni_goruntu": False,
    }
    fresh_excavation = {
        "gorev_id": "FRESH_EXC",
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "ERKEN",
        "mahalle": "Gülbahçe",
        "enlem": 38.410,
        "boylam": 26.650,
        "alan_m2": 600,
        "bolge": east,
        "onceki_tarih": "13.09.2026",
        "son_tarih": "15.09.2026",
        "yeni_goruntu": True,
        "uydu_onceligi": "YÜKSEK",
        "boyut_sinifi": "KUCUK",
        "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
    }
    fresh_plain = {
        "gorev_id": "FRESH_PLAIN",
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "ERKEN",
        "mahalle": "Ildır",
        "enlem": 38.420,
        "boylam": 26.640,
        "alan_m2": 600,
        "bolge": east,
        "onceki_tarih": "13.09.2026",
        "son_tarih": "15.09.2026",
        "yeni_goruntu": True,
        "uydu_onceligi": "YÜKSEK",
        "boyut_sinifi": "KUCUK",
        "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
    }
    old_early = {
        "gorev_id": "OLD_EARLY",
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "ERKEN",
        "mahalle": "Çeşme",
        "enlem": 38.320,
        "boylam": 26.310,
        "alan_m2": 500,
        "bolge": west,
        "yeni_goruntu": False,
        "tarihsel_esleme_mesafe_m": 7.5,
    }
    broad = {
        "gorev_id": "BROAD",
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "ERKEN",
        "mahalle": "Ildır",
        "enlem": 38.430,
        "boylam": 26.580,
        "alan_m2": 12_000,
        "bolge": east,
        "son_tarih": "15.09.2026",
        "yeni_goruntu": True,
        "uydu_onceligi": "YÜKSEK",
    }
    micro = {
        "gorev_id": "MICRO_SHOULD_NOT_PROMOTE",
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "ERKEN",
        "mahalle": "Uzunkuyu",
        "enlem": 38.360,
        "boylam": 26.520,
        "alan_m2": 200,
        "bolge": east,
        "son_tarih": "15.09.2026",
        "yeni_goruntu": True,
        "uydu_onceligi": "YÜKSEK",
        "boyut_sinifi": "KUCUK",
        "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
    }
    micro_history = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "adaylar": [
            {
                "mikro_iz_id": "MTEST_PRECURSOR",
                "alarm": False,
                "saha_gorevi": False,
                "enlem": 38.4101,
                "boylam": 26.6500,
                "ilk_enlem": 38.4101,
                "ilk_boylam": 26.6500,
                "alan_m2": 200,
                "son_guclu_gorulme_tarihi": "03.09.2026",
                "son_guclu_sentinel_item": "SCENE_MICRO_OLD",
                "farkli_sentinel_sahnesi_gorulme_sayisi": 1,
            }
        ],
    }
    field_history = [
        {
            "gorev_id": "FIELD_PRECURSOR",
            "sonuc": "YIKIM_TEMIZLIK",
            "enlem": 38.4101,
            "boylam": 26.6500,
            "alan_m2": 400,
            "son_tarih": "08.09.2026",
        }
    ]

    assert _is_fresh_excavation_candidate(fresh_excavation)
    broad_geometry = dict(fresh_excavation)
    broad_geometry["gorev_id"] = "FRESH_BROAD_GEOMETRY"
    broad_geometry["genis_geometri_riski"] = True
    assert not _is_fresh_excavation_candidate(broad_geometry)
    preseason_scene = dict(fresh_excavation)
    preseason_scene["gorev_id"] = "PRESEASON_SCENE"
    preseason_scene["son_tarih"] = "14.09.2026"
    assert not _is_fresh_excavation_candidate(preseason_scene)
    assert not _is_fresh_excavation_candidate(old_early)
    assert not _is_fresh_excavation_candidate(broad)
    assert not _is_fresh_excavation_candidate(micro)

    field_match = _field_precursor_match(fresh_excavation, field_history)
    assert field_match and field_match["saha_oncul_mesafe_m"] <= FIELD_PRECURSOR_MAX_DISTANCE_M
    assert _field_precursor_match(fresh_plain, field_history) is None
    assert _field_precursor_match(micro, field_history) is None

    wrong_outcome = json.loads(json.dumps(field_history))
    wrong_outcome[0]["sonuc"] = "TARLA_BITKI"
    assert _field_precursor_match(fresh_excavation, wrong_outcome) is None
    same_day_field = json.loads(json.dumps(field_history))
    same_day_field[0]["son_tarih"] = "15.09.2026"
    assert _field_precursor_match(fresh_excavation, same_day_field) is None
    stale_field = json.loads(json.dumps(field_history))
    stale_field[0]["son_tarih"] = "01.07.2026"
    assert _field_precursor_match(fresh_excavation, stale_field) is None

    memory_db = sqlite3.connect(":memory:")
    try:
        memory_db.execute(
            """CREATE TABLE saha_sonuclari (
            gorev_id TEXT, sonuc TEXT, enlem REAL, boylam REAL,
            alan_m2 REAL, son_tarih TEXT)"""
        )
        memory_db.execute(
            "INSERT INTO saha_sonuclari VALUES (?,?,?,?,?,?)",
            ("FIELD_DB_TEST", "YIKIM_TEMIZLIK", 38.4101, 26.6500, 400, "08.09.2026"),
        )
        loaded_field = _field_precursors_from_connection(memory_db)
        assert len(loaded_field) == 1 and loaded_field[0]["gorev_id"] == "FIELD_DB_TEST"
    finally:
        memory_db.close()

    match = _micro_precursor_match(fresh_excavation, micro_history)
    assert match and match["mikro_oncul_mesafe_m"] <= MICRO_PRECURSOR_MAX_DISTANCE_M
    assert _micro_precursor_match(fresh_plain, micro_history) is None
    assert _micro_precursor_match(micro, micro_history) is None

    same_day_history = json.loads(json.dumps(micro_history))
    same_day_history["adaylar"][0]["son_guclu_gorulme_tarihi"] = "15.09.2026"
    assert _micro_precursor_match(fresh_excavation, same_day_history) is None

    ranked_field = select_postseason_shortlist(
        [fresh_plain, fresh_excavation],
        limit=2,
        local_day=date(2026, 9, 15),
        micro_watchlist={},
        field_precursors=field_history,
    )
    assert ranked_field[0]["gorev_id"] == "FRESH_EXC", ranked_field
    assert ranked_field[0]["saha_oncul_eslesmesi"] is True
    assert "saha_oncul_eslesmesi" not in ranked_field[1]

    ranked_fresh = select_postseason_shortlist(
        [fresh_plain, fresh_excavation],
        limit=2,
        local_day=date(2026, 9, 15),
        micro_watchlist=micro_history,
        field_precursors=field_history,
    )
    assert ranked_fresh[0]["gorev_id"] == "FRESH_EXC", ranked_fresh
    assert ranked_fresh[0]["saha_oncul_eslesmesi"] is True
    assert ranked_fresh[0]["mikro_oncul_iz_eslesmesi"] is True
    assert "mikro_oncul_iz_eslesmesi" not in ranked_fresh[1]

    selected = select_postseason_shortlist(
        [old_early, broad, fresh_excavation, repeat, micro],
        limit=3,
        local_day=date(2026, 9, 15),
        micro_watchlist=micro_history,
        field_precursors=field_history,
    )
    ids = [item["gorev_id"] for item in selected]
    assert ids[0] == "REPEAT", ids
    assert ids[1] == "FRESH_EXC", ids
    assert selected[1]["saha_oncul_eslesmesi"] is True
    assert selected[1]["mikro_oncul_iz_eslesmesi"] is True
    assert "MICRO_SHOULD_NOT_PROMOTE" not in ids[:2], ids

    # 15 Eylül öncesi üretim uygulaması no-op olmalı; eşiklerin sabit olduğunu da kilitle.
    assert date(2026, 9, 14) < FULL_OPERATION_START
    assert MAIN_ALARM_MIN_M2 == 250
    assert MICRO_MIN_M2 == 150 and MICRO_MAX_M2 == 249
    print("OK: 15 Eylül sonrası taze hafriyat + saha/MİKRO öncül iz self-check geçti.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()

    if args.self_check:
        _self_check()
        return

    changed, shortlist = apply_postseason_priority()
    if _local_day() < FULL_OPERATION_START:
        print("Kalibrasyon modu: 15 Eylül öncesi rapora dokunulmadı.")
        return
    promoted = sum(_is_fresh_excavation_candidate(item) for item in shortlist)
    field_precursor = sum(bool(item.get("saha_oncul_eslesmesi")) for item in shortlist)
    micro_precursor = sum(bool(item.get("mikro_oncul_iz_eslesmesi")) for item in shortlist)
    print(
        f"15 Eylül sonrası taze hafriyat önceliği uygulandı: "
        f"{promoted} taze ana-alarm adayı ilk üçte, {field_precursor} saha öncülü ve "
        f"{micro_precursor} MİKRO öncül iz eşleşmesi."
    )
    if not changed:
        print("Rapor zaten güncel.")


if __name__ == "__main__":
    main()
