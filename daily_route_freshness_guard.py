"""Günün ilk üç saha kontrolünde güncel Sentinel kanıtını eski taşınmış ölçüye tercih eder.

Bu post-process katmanı yeni alarm veya görev üretmez; tam saha kuyruğunu değiştirmez.
``hydrate_persisted_satellite_metadata`` yalnız son analizde artık görünmeyen açık uydu
görevlerine ``tarihsel_esleme_mesafe_m`` ekler. Aynı GECİKEN şantiye ölçeği sınıfında
bu açık provenans işareti bulunan adayları, halen güncel Sentinel kümesinde görünen
adayların arkasına koyar. Ölçek sınıfı önceliği korunur: tarihsel küçük-güçlü aday,
güncel ama daha geniş bir parsel adayının sırf eski olduğu için arkasına düşmez.

15 Eylül 2026 öncesindeki kalibrasyon döneminde saha rotası ayrıca sıkılaştırılır:
eski/gecikmiş taşınmış adaylar yalnız backlog'da kalır; ilk üçe ancak insanın açıkça
TEKRAR_GIT dediği kayıt veya yeni Sentinel görüntüsünde ortaya çıkan güçlü kompakt
erken/parsel sinyali girebilir. Kuru-zemin diagnostik kalibrasyon noktaları bu dönemde
saha ziyareti olarak önerilmez. 15 Eylül ve sonrasında normal taze-kazı rotası otomatik
olarak geri açılır.

Amaç sınırlı saha zamanını mevcut zemin hareketine ayırmak ve inşaat yasağı döneminde
eski kuyruğun ekibi gereksiz yere dolaştırmasını önlemektir.
"""

from __future__ import annotations

from datetime import date, datetime
import json
from zoneinfo import ZoneInfo

import daily_route_shortlist as route


ISTANBUL = ZoneInfo("Europe/Istanbul")
FULL_OPERATION_START = date(2026, 9, 15)
PRESEASON_STRONG_PRIORITIES = {"ERKEN", "PARSEL"}
PRESEASON_SMALL_MAX_M2 = 800
PRESEASON_ALLOWED_SATELLITE_PRIORITIES = {"YÜKSEK", "ORTA"}

NOTE = (
    "Yeni alarm üretmez; taze ERKEN/PARSEL sinyalini gecikmiş backlog'un önünde "
    "tutar. Gecikenlerde küçük-güçlü ve 800–2.000 m² parsel ölçeğini geniş yüzey "
    "hareketlerinden önce kontrol ettirir; aynı ölçek sınıfında güncel Sentinel "
    "kümesinde görünen aday, yalnız tarihsel ölçüsü geri taşınmış adaya tercih edilir. "
    "Bölge dengesi daha yüksek öncelikli adayı düşürmez."
)

PRESEASON_NOTE = (
    "15 Eylül öncesi kalibrasyon modu: eski/gecikmiş uydu backlog'u ilk saha rotasına "
    "çıkarılmaz. Yalnız insanın TEKRAR_GIT dediği kayıt veya yeni Sentinel görüntüsünde "
    "beliren güçlü kompakt ERKEN/PARSEL-küçük saha sinyali gösterilir; diğer kayıtlar "
    "arka planda izlenmeye devam eder."
)

PRESEASON_CALIBRATION_NOTE = (
    "15 Eylül öncesi kuru-zemin diagnostikleri arka planda kalibrasyon havuzunda tutulur; "
    "sırf veri toplamak için ekip rotasına eklenmez."
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


def _preseason_mode(local_day=None):
    return _local_day(local_day) < FULL_OPERATION_START


def _historical_evidence_rank(item):
    """0=güncel/aktif Sentinel kümesi, 1=tarihsel ölçüsü geri taşınmış açık görev."""
    if not isinstance(item, dict):
        return 0
    if item.get("tarihsel_esleme_mesafe_m") is not None:
        return 1
    reason = str(item.get("oncelik_nedeni") or "").casefold()
    note = str(item.get("konum_notu") or "").casefold()
    if "son analiz kümesinde tekrar görünmedi" in reason:
        return 1
    if "son yeniden analizde küme tekrar görünmedi" in note:
        return 1
    return 0


def _fresh_preseason_candidate(item):
    """Yasak döneminde ancak yeni ve güçlü kanıtı veya insan talebi olan adayı geçir."""
    if not isinstance(item, dict):
        return False

    status = str(item.get("saha_durumu") or "").strip().upper()
    priority = str(item.get("oncelik") or "").strip().upper()
    if status == "TEKRAR_GIT" or priority == "TEKRAR":
        return True

    # Aynı eski Sentinel ölçüsünün günlerce taşınması saha rotası nedeni değildir.
    if item.get("yeni_goruntu") is not True:
        return False
    if _historical_evidence_rank(item) != 0:
        return False

    if priority in PRESEASON_STRONG_PRIORITIES:
        return True

    area = max(_number(item.get("alan_m2"), 0), 0)
    size_class = str(item.get("boyut_sinifi") or "").strip().upper()
    satellite_priority = str(item.get("uydu_onceligi") or "").strip().upper()
    signal = str(item.get("sinyal") or "").casefold()
    strong_small = (
        250 <= area <= PRESEASON_SMALL_MAX_M2
        and (size_class == "KUCUK" or "küçük, güçlü" in signal)
        and satellite_priority in PRESEASON_ALLOWED_SATELLITE_PRIORITIES
    )
    return strong_small


def _fresh_route_sort_key(item, original_index):
    base = route._route_sort_key(item, original_index)
    is_overdue = str(item.get("oncelik") or "").strip().upper() == "GECİKEN"
    freshness = _historical_evidence_rank(item) if is_overdue else 0
    # Base sıra: (öncelik, kazı sınıfı, alan, uydu önceliği, -bekleme, sıra).
    # Güncellik yalnız aynı GECİKEN kazı sınıfı içinde alanın önüne girer.
    return (base[0], base[1], freshness, *base[2:])


def select_fresh_shortlist(candidates, limit=route.SHORTLIST_LIMIT, local_day=None):
    cap = max(int(limit), 0)
    if cap <= 0:
        return []

    eligible = route._actionable_candidates(candidates)
    if _preseason_mode(local_day):
        eligible = [item for item in eligible if _fresh_preseason_candidate(item)]

    indexed = list(enumerate(eligible))
    indexed.sort(key=lambda pair: _fresh_route_sort_key(pair[1], pair[0]))
    ranked = [item for _, item in indexed]

    selected = [dict(item) for item in ranked[:cap]]
    selected = route._balance_satellite_regions(ranked, selected, cap)
    for index, item in enumerate(selected, start=1):
        item["gunluk_sira"] = index
    return selected


def _shortlist_markdown(shortlist, note, empty_message):
    lines = [route.SECTION_TITLE, "", f"> {note}", ""]
    if not shortlist:
        lines.extend([empty_message, ""])
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
        lines.append(
            f"{order}. **{priority} — {neighborhood}**{area_text} · Görev `{task_id}`{route_text}"
        )
    lines.append("")
    return "\n".join(lines)


def _preseason_calibration_markdown():
    return "\n".join(
        [
            route.CALIBRATION_SECTION_TITLE,
            "",
            f"> {PRESEASON_CALIBRATION_NOTE}",
            "",
            "Bugün ekip gönderilecek ek kalibrasyon noktası yok.",
            "",
        ]
    )


def update_fresh_shortlist(local_day=None):
    day = _local_day(local_day)
    preseason = _preseason_mode(day)
    payload = json.loads(route.REPORT_JSON.read_text(encoding="utf-8"))
    candidates = payload.get("saha_adaylari", [])
    shortlist = select_fresh_shortlist(candidates, local_day=day)
    payload["gunun_ilk_3_kontrolu"] = shortlist
    payload["gunun_ilk_3_notu"] = PRESEASON_NOTE if preseason else NOTE

    if preseason:
        payload["kuru_zemin_kalibrasyon_kontrolu"] = []
        payload["kuru_zemin_kalibrasyon_notu"] = PRESEASON_CALIBRATION_NOTE

    route.REPORT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if route.FIELD_REPORT_MD.exists():
        current = route.FIELD_REPORT_MD.read_text(encoding="utf-8")
        empty_message = (
            "Bugün ekip göndermeyi gerektiren güçlü yeni aday yok."
            if preseason
            else "Bugün için eyleme dönük aktif uydu görevi yok."
        )
        updated = route._inject_markdown(
            current,
            _shortlist_markdown(
                shortlist,
                PRESEASON_NOTE if preseason else NOTE,
                empty_message,
            ),
        )
        if preseason:
            updated = route._inject_calibration_markdown(
                updated,
                _preseason_calibration_markdown(),
            )
        else:
            calibration = payload.get("kuru_zemin_kalibrasyon_kontrolu") or []
            updated = route._inject_calibration_markdown(
                updated,
                route._calibration_markdown(calibration),
            )
        route.FIELD_REPORT_MD.write_text(updated, encoding="utf-8")
    return shortlist


def _self_check():
    route._self_check()
    west = route.SATELLITE_REGION_LABELS[0]
    east = route.SATELLITE_REGION_LABELS[1]

    historical_small = {
        "gorev_id": "HIST_SMALL",
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "GECİKEN",
        "mahalle": "Musalla",
        "enlem": 38.2947,
        "boylam": 26.3115,
        "alan_m2": 300,
        "bolge": west,
        "bekleme_gun": 5,
        "uydu_onceligi": "ORTA",
        "boyut_sinifi": "KUCUK",
        "sinyal": "5 gündür saha kontrolü bekliyor · Küçük, güçlü yüzey/toprak değişimi adayı",
        "tarihsel_esleme_mesafe_m": 8.9,
    }
    current_small = {
        "gorev_id": "CURRENT_SMALL",
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "GECİKEN",
        "mahalle": "Şifne",
        "enlem": 38.3460,
        "boylam": 26.3834,
        "alan_m2": 400,
        "bolge": west,
        "bekleme_gun": 3,
        "uydu_onceligi": "ORTA",
        "boyut_sinifi": "KUCUK",
        "sinyal": "3 gündür saha kontrolü bekliyor · Küçük, güçlü yüzey/toprak değişimi adayı",
    }
    current_east = {
        "gorev_id": "CURRENT_EAST",
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "GECİKEN",
        "mahalle": "Ildır",
        "enlem": 38.4257,
        "boylam": 26.5774,
        "alan_m2": 400,
        "bolge": east,
        "bekleme_gun": 3,
        "uydu_onceligi": "ORTA",
        "boyut_sinifi": "KUCUK",
        "sinyal": "3 gündür saha kontrolü bekliyor · Küçük, güçlü yüzey/toprak değişimi adayı",
    }
    current_parcel = {
        "gorev_id": "CURRENT_PARCEL",
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "GECİKEN",
        "mahalle": "Ovacık",
        "enlem": 38.2237,
        "boylam": 26.3930,
        "alan_m2": 800,
        "bolge": west,
        "bekleme_gun": 2,
        "uydu_onceligi": "NORMAL",
        "boyut_sinifi": "STANDART",
        "sinyal": "2 gündür saha kontrolü bekliyor · parsel ölçekli yüzey/toprak değişimi adayı",
    }

    # 15 Eylül ve sonrasında önceki güncellik sıralaması aynen korunur.
    chosen = select_fresh_shortlist(
        [historical_small, current_small, current_east, current_parcel],
        limit=3,
        local_day=date(2026, 9, 15),
    )
    ids = [item["gorev_id"] for item in chosen]
    assert ids[:2] == ["CURRENT_SMALL", "CURRENT_EAST"], ids
    assert ids[2] == "HIST_SMALL", ids
    assert "CURRENT_PARCEL" not in ids, "Küçük-güçlü sınıf parsel sınıfının arkasına düşmemeli."
    assert all(item["gunluk_sira"] == index for index, item in enumerate(chosen, 1))

    one_region = select_fresh_shortlist(
        [historical_small, current_small],
        limit=2,
        local_day=date(2026, 9, 15),
    )
    assert [item["gorev_id"] for item in one_region] == ["CURRENT_SMALL", "HIST_SMALL"]

    # 15 Eylül öncesi eski/gecikmiş ölçü rota nedeni değildir. İnsan TEKRAR_GIT
    # ve yeni görüntüdeki güçlü kompakt aday ise kaçırılmamalıdır.
    fresh_early = {
        "gorev_id": "FRESH_EARLY",
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "ERKEN",
        "mahalle": "Gülbahçe",
        "enlem": 38.3195,
        "boylam": 26.6465,
        "alan_m2": 600,
        "bolge": east,
        "uydu_onceligi": "YÜKSEK",
        "boyut_sinifi": "KUCUK",
        "yeni_goruntu": True,
    }
    manual_repeat = {
        "gorev_id": "MANUAL_REPEAT",
        "saha_durumu": "TEKRAR_GIT",
        "oncelik": "TEKRAR",
        "mahalle": "Alaçatı",
        "enlem": 38.2848,
        "boylam": 26.3745,
        "alan_m2": 500,
        "bolge": west,
        "yeni_goruntu": False,
    }
    preseason = select_fresh_shortlist(
        [historical_small, current_small, fresh_early, manual_repeat],
        limit=3,
        local_day=date(2026, 9, 2),
    )
    preseason_ids = [item["gorev_id"] for item in preseason]
    assert preseason_ids == ["MANUAL_REPEAT", "FRESH_EARLY"], preseason_ids
    assert "HIST_SMALL" not in preseason_ids
    assert "CURRENT_SMALL" not in preseason_ids
    assert _preseason_mode(date(2026, 9, 14)) is True
    assert _preseason_mode(date(2026, 9, 15)) is False


if __name__ == "__main__":
    _self_check()
    chosen = update_fresh_shortlist()
    print(
        "Günün ilk kontrolünde güncel uydu kanıtı önceliklendirildi: "
        + (", ".join(str(item.get("gorev_id")) for item in chosen) or "güçlü yeni saha adayı yok")
    )
