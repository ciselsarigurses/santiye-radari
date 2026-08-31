"""Günün ilk üç saha kontrolünde güncel Sentinel kanıtını eski taşınmış ölçüye tercih eder.

Bu post-process katmanı yeni alarm veya görev üretmez; tam saha kuyruğunu değiştirmez.
``hydrate_persisted_satellite_metadata`` yalnız son analizde artık görünmeyen açık uydu
görevlerine ``tarihsel_esleme_mesafe_m`` ekler. Aynı GECİKEN şantiye ölçeği sınıfında
bu açık provenans işareti bulunan adayları, halen güncel Sentinel kümesinde görünen
adayların arkasına koyar. Ölçek sınıfı önceliği korunur: tarihsel küçük-güçlü aday,
güncel ama daha geniş bir parsel adayının sırf eski olduğu için arkasına düşmez.

Amaç saha ekibinin sınırlı ilk üç ziyaretini mümkün olduğunca halen görünen zemin
hareketine ayırmak; eski açık görevi ise tam listede saha teyidi beklemeye devam
ettirmektir.
"""

from __future__ import annotations

import json

import daily_route_shortlist as route


NOTE = (
    "Yeni alarm üretmez; taze ERKEN/PARSEL sinyalini gecikmiş backlog'un önünde "
    "tutar. Gecikenlerde küçük-güçlü ve 800–2.000 m² parsel ölçeğini geniş yüzey "
    "hareketlerinden önce kontrol ettirir; aynı ölçek sınıfında güncel Sentinel "
    "kümesinde görünen aday, yalnız tarihsel ölçüsü geri taşınmış adaya tercih edilir. "
    "Bölge dengesi daha yüksek öncelikli adayı düşürmez."
)


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


def _fresh_route_sort_key(item, original_index):
    base = route._route_sort_key(item, original_index)
    is_overdue = str(item.get("oncelik") or "").strip().upper() == "GECİKEN"
    freshness = _historical_evidence_rank(item) if is_overdue else 0
    # Base sıra: (öncelik, kazı sınıfı, alan, uydu önceliği, -bekleme, sıra).
    # Güncellik yalnız aynı GECİKEN kazı sınıfı içinde alanın önüne girer.
    return (base[0], base[1], freshness, *base[2:])


def select_fresh_shortlist(candidates, limit=route.SHORTLIST_LIMIT):
    cap = max(int(limit), 0)
    if cap <= 0:
        return []

    eligible = route._actionable_candidates(candidates)
    indexed = list(enumerate(eligible))
    indexed.sort(key=lambda pair: _fresh_route_sort_key(pair[1], pair[0]))
    ranked = [item for _, item in indexed]

    selected = [dict(item) for item in ranked[:cap]]
    selected = route._balance_satellite_regions(ranked, selected, cap)
    for index, item in enumerate(selected, start=1):
        item["gunluk_sira"] = index
    return selected


def update_fresh_shortlist():
    payload = json.loads(route.REPORT_JSON.read_text(encoding="utf-8"))
    candidates = payload.get("saha_adaylari", [])
    shortlist = select_fresh_shortlist(candidates)
    payload["gunun_ilk_3_kontrolu"] = shortlist
    payload["gunun_ilk_3_notu"] = NOTE
    route.REPORT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if route.FIELD_REPORT_MD.exists():
        current = route.FIELD_REPORT_MD.read_text(encoding="utf-8")
        updated = route._inject_markdown(current, route._shortlist_markdown(shortlist))
        # _inject_markdown kısa liste ile ana aday bölümü arasındaki alanı yeniler;
        # mevcut kuru-zemin kalibrasyonunu aynı içerikle tekrar ekleyerek kaybetme.
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

    chosen = select_fresh_shortlist(
        [historical_small, current_small, current_east, current_parcel],
        limit=3,
    )
    ids = [item["gorev_id"] for item in chosen]
    assert ids[:2] == ["CURRENT_SMALL", "CURRENT_EAST"], ids
    assert ids[2] == "HIST_SMALL", ids
    assert "CURRENT_PARCEL" not in ids, "Küçük-güçlü sınıf parsel sınıfının arkasına düşmemeli."
    assert all(item["gunluk_sira"] == index for index, item in enumerate(chosen, 1))

    # Aynı tek bölgede de güncellik, tarihsel adayı aynı sınıfta alan farkına
    # rağmen geriye iter; bu tam saha listesini değil yalnız ilk üçü etkiler.
    one_region = select_fresh_shortlist([historical_small, current_small], limit=2)
    assert [item["gorev_id"] for item in one_region] == ["CURRENT_SMALL", "HIST_SMALL"]


if __name__ == "__main__":
    _self_check()
    chosen = update_fresh_shortlist()
    print(
        "Günün ilk kontrolünde güncel uydu kanıtı önceliklendirildi: "
        + (", ".join(str(item.get("gorev_id")) for item in chosen) or "aktif görev yok")
    )
