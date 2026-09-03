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

15 Eylül öncesinde üretim raporuna dokunmaz; yalnız --self-check ile sezon açılışı
davranışı bugünden doğrulanabilir.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from zoneinfo import ZoneInfo

import daily_route_shortlist as route
import daily_route_freshness_guard as freshness


ISTANBUL = ZoneInfo("Europe/Istanbul")
FULL_OPERATION_START = date(2026, 9, 15)
MAIN_ALARM_MIN_M2 = 250
MICRO_MAX_M2 = 249
FRESH_EXCAVATION_MAX_M2 = 5_000
STRONG_SMALL_MAX_M2 = 800
ALLOWED_SATELLITE_PRIORITIES = {"YÜKSEK", "ORTA"}
FRESH_ROUTE_PRIORITIES = {"ERKEN", "PARSEL"}
NOTE = (
    "15 Eylül sonrası operasyon modu: yeni Sentinel görüntüsünde beliren 250 m²+ "
    "kompakt ERKEN/PARSEL ve küçük-güçlü hafriyat/temel sinyalleri eski backlog'un "
    "önüne alınır. TEKRAR_GIT her zaman en yüksek önceliktedir. 150–249 m² Mikro "
    "Şantiye diagnostikleri doğrudan saha görevine yükseltilmez."
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


def _priority_band(item):
    if _is_manual_repeat(item):
        return 0
    if _is_fresh_excavation_candidate(item):
        return 1
    return 2


def _sort_key(item, original_index):
    base = route._route_sort_key(item, original_index)
    return (_priority_band(item), *base)


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


def select_postseason_shortlist(candidates, limit=route.SHORTLIST_LIMIT, local_day=None):
    cap = max(int(limit), 0)
    if cap <= 0:
        return []

    # Legacy Uzunkuyu etiketi ile Gülbahçe-dahil yeni etiketi aynı doğu bölgesi kabul et.
    eligible = freshness._normalized_actionable_candidates(candidates)
    indexed = list(enumerate(eligible))
    indexed.sort(key=lambda pair: _sort_key(pair[1], pair[0]))
    ranked = [item for _, item in indexed]

    selected = [dict(item) for item in ranked[:cap]]
    selected = _balance_regions(ranked, selected, cap)
    for index, item in enumerate(selected, start=1):
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
        lines.append(
            f"{order}. **{priority} — {neighborhood}**{area_text}{fresh_tag} · "
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
    shortlist = select_postseason_shortlist(candidates, local_day=day)
    promoted = [
        str(item.get("gorev_id") or "")
        for item in shortlist
        if _is_fresh_excavation_candidate(item)
    ]

    payload["gunun_ilk_3_kontrolu"] = shortlist
    payload["gunun_ilk_3_notu"] = NOTE
    payload["postseason_excavation_priority"] = {
        "aktif": True,
        "baslangic_tarihi": FULL_OPERATION_START.isoformat(),
        "ana_alarm_alt_esigi_m2": MAIN_ALARM_MIN_M2,
        "taze_kazi_oncelik_ust_siniri_m2": FRESH_EXCAVATION_MAX_M2,
        "mikro_santiye": "150-249 m² diagnostik; doğrudan saha görevine yükseltilmez",
        "one_alinan_gorevler": promoted,
        "not": (
            "Yalnız mevcut aktif görevlerin sırasını değiştirir; alarm/görev üretmez. "
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
        "yeni_goruntu": True,
        "uydu_onceligi": "YÜKSEK",
        "boyut_sinifi": "KUCUK",
        "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
    }

    assert _is_fresh_excavation_candidate(fresh_excavation)
    assert not _is_fresh_excavation_candidate(old_early)
    assert not _is_fresh_excavation_candidate(broad)
    assert not _is_fresh_excavation_candidate(micro)

    selected = select_postseason_shortlist(
        [old_early, broad, fresh_excavation, repeat, micro],
        limit=3,
        local_day=date(2026, 9, 15),
    )
    ids = [item["gorev_id"] for item in selected]
    assert ids[0] == "REPEAT", ids
    assert ids[1] == "FRESH_EXC", ids
    assert "MICRO_SHOULD_NOT_PROMOTE" not in ids[:2], ids

    # 15 Eylül öncesi üretim uygulaması no-op olmalı; eşiklerin sabit olduğunu da kilitle.
    assert date(2026, 9, 14) < FULL_OPERATION_START
    assert MAIN_ALARM_MIN_M2 == 250
    assert MICRO_MAX_M2 == 249
    print("OK: 15 Eylül sonrası taze hafriyat önceliği self-check geçti.")


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
    print(
        f"15 Eylül sonrası taze hafriyat önceliği uygulandı: "
        f"{promoted} taze ana-alarm adayı ilk üçte."
    )
    if not changed:
        print("Rapor zaten güncel.")


if __name__ == "__main__":
    main()
