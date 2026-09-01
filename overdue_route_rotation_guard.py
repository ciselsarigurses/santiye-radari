"""Gecikmiş aktif uydu görevlerinde aynı ilk üç noktanın günlerce kilitlenmesini azaltır.

Bu katman yeni alarm/görev üretmez, görev kapatmaz ve Sentinel eşiklerini değiştirmez.
TEKRAR/ERKEN/PARSEL gibi daha yüksek öncelikli bir görev varsa mevcut günlük kısa listeye
dokunmaz. Yalnız bütün aktif öncelik gecikmiş kuyruğa düştüğünde ve aynı güncel Sentinel
sahnesinden birden fazla eşdeğer küçük-güçlü (250-800 m²) aday varsa çalışır.

En güçlü aday "çapa" olarak her gün korunur. Kalan iki slot, aynı kanıt sınıfındaki
adaylar arasında Çeşme'nin yerel takvim gününe göre deterministik döner. Rapor bir önceki
günün verisini taşısa bile gece yarısından sonra rota eski tarihte kilitli kalmaz. Mümkünse
farklı mahalle ve iki uydu bölgesi temsil edilir. Böylece 61 gibi büyüyen gecikmiş kuyrukta
aynı üç adresi tekrar tekrar göstermek yerine, erken hafriyat niteliğini düşürmeden saha
kapsaması genişler.

Not: Bu yalnız rapordaki ``gunun_ilk_3_kontrolu`` görünümünü değiştirir; tam saha listesi
ve görev durumları aynen kalır.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from zoneinfo import ZoneInfo

import daily_route_shortlist as route
import daily_route_freshness_guard as fresh


LOCAL_TZ = ZoneInfo("Europe/Istanbul")

NOTE = (
    "Yeni alarm üretmez; TEKRAR/ERKEN/PARSEL öncelikleri aynen korunur. Yalnız bütün "
    "kısa liste gecikmiş kuyruğa kaldığında, en güçlü küçük-güçlü güncel Sentinel adayı "
    "çapa olarak sabit tutulur; kalan iki slot aynı kanıt sınıfındaki adaylar arasında "
    "Çeşme yerel takvim gününe göre günlük döner. Mümkünse farklı mahalle ve iki uydu "
    "bölgesi temsil edilir. Tam saha kuyruğu ve görev durumları değişmez."
)


def _parse_date(value):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _report_ordinal(report_date):
    parsed = _parse_date(report_date)
    return parsed.toordinal() if parsed else 0


def _effective_rotation_date(report_date, local_today=None):
    """Rotasyonu bayat rapor tarihine kilitlemeden güvenli, deterministik günü seç."""
    report_day = _parse_date(report_date)
    today = local_today or datetime.now(LOCAL_TZ).date()
    if report_day is None:
        return today
    return max(report_day, today)


def _is_higher_than_overdue(item):
    return route._route_priority_value(item) < route.ROUTE_PRIORITY["GECİKEN"]


def _satellite_rank(item):
    return route.SATELLITE_PRIORITY.get(
        str(item.get("uydu_onceligi") or "").strip().upper(),
        3,
    )


def _small_current_band(ranked):
    """Çapanın kalite bandını düşürmeden döndürülebilecek adayları çıkar."""
    if not ranked:
        return []
    anchor = ranked[0]
    if str(anchor.get("oncelik") or "").strip().upper() != "GECİKEN":
        return []
    if route._overdue_excavation_class(anchor) != 0:
        return []
    if fresh._historical_evidence_rank(anchor) != 0:
        return []

    anchor_satellite_rank = _satellite_rank(anchor)
    anchor_end_date = str(anchor.get("son_tarih") or "")
    pool = []
    for item in ranked:
        if str(item.get("oncelik") or "").strip().upper() != "GECİKEN":
            continue
        if route._overdue_excavation_class(item) != 0:
            continue
        if fresh._historical_evidence_rank(item) != 0:
            continue
        # Daha zayıf uydu sınıfını sırf rotasyon olsun diye öne çekme.
        if _satellite_rank(item) > anchor_satellite_rank:
            continue
        # Farklı son sahne tarihini aynı kanıt bandı sayma.
        if anchor_end_date and str(item.get("son_tarih") or "") != anchor_end_date:
            continue
        pool.append(item)
    return pool


def _rotate_after_anchor(pool, rotation_date, limit):
    if not pool or limit <= 0:
        return []
    anchor = dict(pool[0])
    if limit == 1:
        return [anchor]

    alternatives = list(pool[1:])
    if not alternatives:
        return [anchor]

    offset = _report_ordinal(rotation_date) % len(alternatives)
    circular = alternatives[offset:] + alternatives[:offset]
    selected = [anchor]
    used_ids = {str(anchor.get("gorev_id") or "")}
    used_neighborhoods = {str(anchor.get("mahalle") or "").casefold().strip()}

    # Önce farklı mahalleleri doldur.
    for item in circular:
        if len(selected) >= limit:
            break
        task_id = str(item.get("gorev_id") or "")
        neighborhood = str(item.get("mahalle") or "").casefold().strip()
        if task_id in used_ids or (neighborhood and neighborhood in used_neighborhoods):
            continue
        selected.append(dict(item))
        used_ids.add(task_id)
        if neighborhood:
            used_neighborhoods.add(neighborhood)

    # Mahalle çeşitliliği yetmezse aynı kalite bandından kalanlarla tamamla.
    for item in circular:
        if len(selected) >= limit:
            break
        task_id = str(item.get("gorev_id") or "")
        if task_id in used_ids:
            continue
        selected.append(dict(item))
        used_ids.add(task_id)

    return selected


def select_rotated_shortlist(candidates, rotation_date, limit=route.SHORTLIST_LIMIT):
    cap = max(int(limit), 0)
    if cap <= 0:
        return []

    eligible = route._actionable_candidates(candidates)
    indexed = list(enumerate(eligible))
    indexed.sort(key=lambda pair: fresh._fresh_route_sort_key(pair[1], pair[0]))
    ranked = [item for _, item in indexed]
    if not ranked:
        return []

    # Taze/insan teyitli daha yüksek öncelik varsa mevcut güvenli davranışı koru.
    if any(_is_higher_than_overdue(item) for item in ranked):
        return fresh.select_fresh_shortlist(candidates, limit=cap)

    pool = _small_current_band(ranked)
    if len(pool) <= 2:
        return fresh.select_fresh_shortlist(candidates, limit=cap)

    selected = _rotate_after_anchor(pool, rotation_date, cap)
    # İki bölge aynı kalite düzeyinde mevcutsa mevcut bölge dengelemesini koru.
    selected = route._balance_satellite_regions(pool, selected, cap)

    pool_ids = {str(item.get("gorev_id") or "") for item in pool}
    ordinal = _report_ordinal(rotation_date)
    for index, item in enumerate(selected, start=1):
        item["gunluk_sira"] = index
        if str(item.get("gorev_id") or "") in pool_ids:
            item["gecikmis_rota_rotasyonu"] = True
            item["gecikmis_rota_rotasyon_gun"] = ordinal
            item["gecikmis_rota_rotasyon_havuzu"] = len(pool)
            item["gecikmis_rota_capa"] = index == 1
    return selected


def _rotation_markdown(shortlist):
    text = route._shortlist_markdown(shortlist)
    default_note = (
        "> Bu bölüm yeni alarm üretmez. Taze ERKEN/PARSEL sinyalini gecikmiş backlog'un "
        "önünde tutar; gecikenlerde küçük-güçlü ve parsel ölçeğini geniş yüzey "
        "hareketlerinden önce kontrol ettirir. İki uydu bölgesi dengesi yalnız daha "
        "yüksek öncelikli adayı düşürmeden uygulanır."
    )
    return text.replace(default_note, "> " + NOTE)


def update_rotated_shortlist():
    payload = json.loads(route.REPORT_JSON.read_text(encoding="utf-8"))
    candidates = payload.get("saha_adaylari", [])
    report_date = payload.get("rapor_tarihi")
    rotation_date = _effective_rotation_date(report_date)
    shortlist = select_rotated_shortlist(candidates, rotation_date.isoformat())

    payload["gunun_ilk_3_kontrolu"] = shortlist
    payload["gunun_ilk_3_notu"] = NOTE
    payload["gecikmis_rota_rotasyonu"] = {
        "durum": "uygulandi"
        if any(item.get("gecikmis_rota_rotasyonu") for item in shortlist)
        else "gerekmedi",
        "kural": (
            "Yalniz gecikmis kuyrukta, ayni guncel Sentinel kalite bandindaki "
            "kucuk-guclu adaylarda en guclu capa korunur; kalan slotlar Cesme yerel "
            "takvim gunune gore gunluk doner."
        ),
        "havuz": max(
            (int(item.get("gecikmis_rota_rotasyon_havuzu") or 0) for item in shortlist),
            default=0,
        ),
        "rapor_tarihi": str(report_date or ""),
        "rotasyon_tarihi": rotation_date.isoformat(),
    }
    route.REPORT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if route.FIELD_REPORT_MD.exists():
        current = route.FIELD_REPORT_MD.read_text(encoding="utf-8")
        updated = route._inject_markdown(current, _rotation_markdown(shortlist))
        calibration = payload.get("kuru_zemin_kalibrasyon_kontrolu") or []
        updated = route._inject_calibration_markdown(
            updated,
            route._calibration_markdown(calibration),
        )
        route.FIELD_REPORT_MD.write_text(updated, encoding="utf-8")
    return shortlist


def _self_check():
    west = route.SATELLITE_REGION_LABELS[0]
    east = route.SATELLITE_REGION_LABELS[1]

    def item(task_id, neighborhood, area, region, satellite="ORTA"):
        return {
            "gorev_id": task_id,
            "saha_durumu": "KONTROLE_GIT",
            "oncelik": "GECİKEN",
            "mahalle": neighborhood,
            "enlem": 38.2,
            "boylam": 26.4,
            "alan_m2": area,
            "bolge": region,
            "bekleme_gun": 3,
            "uydu_onceligi": satellite,
            "boyut_sinifi": "KUCUK",
            "sinyal": "3 gündür saha kontrolü bekliyor · Küçük, güçlü yüzey/toprak değişimi adayı",
            "son_tarih": "29.08.2026",
        }

    sample = [
        item("ANCHOR", "Şifne", 300, west),
        item("ALA1", "Alaçatı", 400, west),
        item("ILDIR", "Ildır", 400, east),
        item("OVACIK", "Ovacık", 500, west),
        item("ALA2", "Alaçatı", 600, west),
    ]
    day1 = select_rotated_shortlist(sample, "2026-09-01", limit=3)
    day2 = select_rotated_shortlist(sample, "2026-09-02", limit=3)
    assert day1[0]["gorev_id"] == "ANCHOR", day1
    assert day2[0]["gorev_id"] == "ANCHOR", day2
    assert {entry["bolge"] for entry in day1} == {west, east}, day1
    assert {entry["bolge"] for entry in day2} == {west, east}, day2
    assert len({entry["mahalle"] for entry in day1}) == 3, day1
    assert len({entry["mahalle"] for entry in day2}) == 3, day2
    assert [entry["gorev_id"] for entry in day1] != [entry["gorev_id"] for entry in day2]
    assert all(entry.get("gecikmis_rota_rotasyonu") for entry in day1)

    # Bayat rapor gece yarısından sonra rotasyonu eski günde tutmamalı.
    effective = _effective_rotation_date("2026-09-01", date(2026, 9, 2))
    assert effective.isoformat() == "2026-09-02"
    future_safe = _effective_rotation_date("2026-09-03", date(2026, 9, 2))
    assert future_safe.isoformat() == "2026-09-03"
    missing_safe = _effective_rotation_date(None, date(2026, 9, 2))
    assert missing_safe.isoformat() == "2026-09-02"

    early = dict(sample[1])
    early.update({"gorev_id": "EARLY", "oncelik": "ERKEN"})
    unchanged = select_rotated_shortlist([early, *sample], "2026-09-02", limit=3)
    expected = fresh.select_fresh_shortlist([early, *sample], limit=3)
    assert [x["gorev_id"] for x in unchanged] == [x["gorev_id"] for x in expected]

    historical = item("HIST", "Musalla", 250, west)
    historical["tarihsel_esleme_mesafe_m"] = 5.0
    safe = select_rotated_shortlist([historical, *sample], "2026-09-02", limit=3)
    assert safe[0]["gorev_id"] == "ANCHOR", safe
    assert "HIST" not in {entry["gorev_id"] for entry in safe}, safe

    weaker = item("WEAK", "Germiyan", 350, east, satellite="NORMAL")
    pool = _small_current_band(
        [sample[0], weaker, sample[1], sample[2]]
    )
    assert "WEAK" not in {entry["gorev_id"] for entry in pool}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Gecikmiş rota rotasyonu öz testi başarılı.")
        return
    chosen = update_rotated_shortlist()
    print(
        "Gecikmiş günlük rota rotasyonu uygulandı: "
        + (", ".join(str(item.get("gorev_id")) for item in chosen) or "aktif görev yok")
    )


if __name__ == "__main__":
    main()
