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
olan aynı küçük alan, sonraki yeni sahnede bağımsız olarak 250 m²+ ana adaya dönüşürse
bu geçmiş yalnız taze-kazı bandı içinde sıralama kanıtı olarak kullanılır. MİKRO iz
tek başına alarm veya saha görevi üretmez.

15 Eylül öncesinde üretim raporuna dokunmaz; yalnız --self-check ile sezon açılışı
davranışı bugünden doğrulanabilir.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import daily_route_shortlist as route
import daily_route_freshness_guard as freshness


ISTANBUL = ZoneInfo("Europe/Istanbul")
BASE = Path(__file__).resolve().parent
MICRO_WATCHLIST_FILE = BASE / "micro_site_watchlist.json"
FULL_OPERATION_START = date(2026, 9, 15)
MAIN_ALARM_MIN_M2 = 250
MICRO_MIN_M2 = 150
MICRO_MAX_M2 = 249
MICRO_PRECURSOR_MAX_DISTANCE_M = 25.0
FRESH_EXCAVATION_MAX_M2 = 5_000
STRONG_SMALL_MAX_M2 = 800
ALLOWED_SATELLITE_PRIORITIES = {"YÜKSEK", "ORTA"}
FRESH_ROUTE_PRIORITIES = {"ERKEN", "PARSEL"}
NOTE = (
    "15 Eylül sonrası operasyon modu: yeni Sentinel görüntüsünde beliren 250 m²+ "
    "kompakt ERKEN/PARSEL ve küçük-güçlü hafriyat/temel sinyalleri eski backlog'un "
    "önüne alınır. Aynı küçük alanda daha eski güçlü 150–249 m² MİKRO izi varsa, "
    "bağımsız 250 m²+ taze aday kendi taze-kazı bandı içinde öne alınır. TEKRAR_GIT "
    "her zaman en yüksek önceliktedir; MİKRO iz tek başına saha görevine yükseltilmez."
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
    if item.get("genis_geometri_riski") is True and area >= 10_000:
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


def _sort_key(item, original_index, micro_watchlist=None):
    base = route._route_sort_key(item, original_index)
    band = _priority_band(item)
    micro_rank = 0 if band == 1 and _micro_precursor_match(item, micro_watchlist or {}) else 1
    return (band, micro_rank, *base)


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
):
    cap = max(int(limit), 0)
    if cap <= 0:
        return []

    micro_watchlist = micro_watchlist if isinstance(micro_watchlist, dict) else {}
    # Legacy Uzunkuyu etiketi ile Gülbahçe-dahil yeni etiketi aynı doğu bölgesi kabul et.
    eligible = freshness._normalized_actionable_candidates(candidates)
    indexed = list(enumerate(eligible))
    indexed.sort(key=lambda pair: _sort_key(pair[1], pair[0], micro_watchlist))
    ranked = [item for _, item in indexed]

    selected = [dict(item) for item in ranked[:cap]]
    selected = _balance_regions(ranked, selected, cap)
    for index, item in enumerate(selected, start=1):
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
        micro_tag = " · **MİKRO→ANA DEVAM KANITI**" if item.get("mikro_oncul_iz_eslesmesi") else ""
        lines.append(
            f"{order}. **{priority} — {neighborhood}**{area_text}{fresh_tag}{micro_tag} · "
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
    shortlist = select_postseason_shortlist(
        candidates,
        local_day=day,
        micro_watchlist=micro_watchlist,
    )
    promoted = [
        str(item.get("gorev_id") or "")
        for item in shortlist
        if _is_fresh_excavation_candidate(item)
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
        "one_alinan_gorevler": promoted,
        "not": (
            "Yalnız mevcut aktif görevlerin sırasını değiştirir; alarm/görev üretmez. "
            "Daha eski güçlü MİKRO izi yalnız zaten bağımsız 250 m²+ taze ana adayda "
            "sıralama kanıtıdır. TEKRAR_GIT en yüksek öncelikte kalır."
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

    assert _is_fresh_excavation_candidate(fresh_excavation)
    preseason_scene = dict(fresh_excavation)
    preseason_scene["gorev_id"] = "PRESEASON_SCENE"
    preseason_scene["son_tarih"] = "14.09.2026"
    assert not _is_fresh_excavation_candidate(preseason_scene)
    assert not _is_fresh_excavation_candidate(old_early)
    assert not _is_fresh_excavation_candidate(broad)
    assert not _is_fresh_excavation_candidate(micro)

    match = _micro_precursor_match(fresh_excavation, micro_history)
    assert match and match["mikro_oncul_mesafe_m"] <= MICRO_PRECURSOR_MAX_DISTANCE_M
    assert _micro_precursor_match(fresh_plain, micro_history) is None
    assert _micro_precursor_match(micro, micro_history) is None

    same_day_history = json.loads(json.dumps(micro_history))
    same_day_history["adaylar"][0]["son_guclu_gorulme_tarihi"] = "15.09.2026"
    assert _micro_precursor_match(fresh_excavation, same_day_history) is None

    ranked_fresh = select_postseason_shortlist(
        [fresh_plain, fresh_excavation],
        limit=2,
        local_day=date(2026, 9, 15),
        micro_watchlist=micro_history,
    )
    assert ranked_fresh[0]["gorev_id"] == "FRESH_EXC", ranked_fresh
    assert ranked_fresh[0]["mikro_oncul_iz_eslesmesi"] is True
    assert "mikro_oncul_iz_eslesmesi" not in ranked_fresh[1]

    selected = select_postseason_shortlist(
        [old_early, broad, fresh_excavation, repeat, micro],
        limit=3,
        local_day=date(2026, 9, 15),
        micro_watchlist=micro_history,
    )
    ids = [item["gorev_id"] for item in selected]
    assert ids[0] == "REPEAT", ids
    assert ids[1] == "FRESH_EXC", ids
    assert selected[1]["mikro_oncul_iz_eslesmesi"] is True
    assert "MICRO_SHOULD_NOT_PROMOTE" not in ids[:2], ids

    # 15 Eylül öncesi üretim uygulaması no-op olmalı; eşiklerin sabit olduğunu da kilitle.
    assert date(2026, 9, 14) < FULL_OPERATION_START
    assert MAIN_ALARM_MIN_M2 == 250
    assert MICRO_MIN_M2 == 150 and MICRO_MAX_M2 == 249
    print("OK: 15 Eylül sonrası taze hafriyat + MİKRO öncül iz self-check geçti.")


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
    precursor = sum(bool(item.get("mikro_oncul_iz_eslesmesi")) for item in shortlist)
    print(
        f"15 Eylül sonrası taze hafriyat önceliği uygulandı: "
        f"{promoted} taze ana-alarm adayı ilk üçte, {precursor} MİKRO öncül iz eşleşmesi."
    )
    if not changed:
        print("Rapor zaten güncel.")


if __name__ == "__main__":
    main()
