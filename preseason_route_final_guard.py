"""15 Eylül öncesi saha rotası invariantını post-process zincirinin sonunda korur.

Günlük taramadaki ``daily_route_freshness_guard`` yasak/kalibrasyon döneminde eski
Sentinel backlog'unu ilk üç saha rotasından çıkarır. Kör-alan ve kalibrasyon workflow'u
daha sonra raporu yeniden işlediği için gecikmiş rota rotasyonu bu sonucu yanlışlıkla
geri yazabilir. Bu son koruma yalnız 15 Eylül 2026 öncesinde çalışır ve aynı taze-kanıt
kuralını yeniden uygular.

Yeni alarm veya görev üretmez; görev durumlarını, 250 m² ana eşiği, mikro diagnostik
katmanını ve kör-alan verilerini değiştirmez. Markdown'da yalnız ``Günün ilk 3`` ve
``Ek kuru zemin kalibrasyon`` bölümlerini değiştirir; aradaki/sonraki kapasite, kör-alan
ve diğer diagnostik başlıkları korur. 15 Eylül ve sonrasında no-op olur.
"""

from __future__ import annotations

import argparse
import json
from datetime import date

import daily_route_freshness_guard as fresh
import daily_route_shortlist as route


PRESEASON_EMPTY_MESSAGE = "Bugün ekip göndermeyi gerektiren güçlü yeni aday yok."


def _next_heading_index(text, start):
    marker = text.find("\n## ", max(int(start), 0))
    return marker + 1 if marker >= 0 else len(text)


def _replace_heading_section(text, heading, section, insert_before=None):
    """Tek bir H2 bölümünü değiştir; takip eden diagnostik bölümleri silme."""
    source = str(text or "")
    rendered = str(section or "").rstrip() + "\n"
    start = source.find(heading)
    if start >= 0:
        end = _next_heading_index(source, start + len(heading))
        prefix = source[:start].rstrip()
        suffix = source[end:].lstrip("\n")
        if prefix:
            return prefix + "\n\n" + rendered.rstrip() + "\n\n" + suffix
        return rendered.rstrip() + "\n\n" + suffix

    anchors = insert_before or []
    for anchor in anchors:
        marker = source.find(anchor)
        if marker < 0:
            continue
        prefix = source[:marker].rstrip()
        suffix = source[marker:].lstrip("\n")
        if prefix:
            return prefix + "\n\n" + rendered.rstrip() + "\n\n" + suffix
        return rendered.rstrip() + "\n\n" + suffix

    if not source.strip():
        return rendered
    return source.rstrip() + "\n\n" + rendered


def _apply_markdown_guard(text, shortlist):
    guarded = _replace_heading_section(
        text,
        route.SECTION_TITLE,
        fresh._shortlist_markdown(
            shortlist,
            fresh.PRESEASON_NOTE,
            PRESEASON_EMPTY_MESSAGE,
        ),
        insert_before=[route.CALIBRATION_SECTION_TITLE, route.NEXT_SECTION],
    )
    guarded = _replace_heading_section(
        guarded,
        route.CALIBRATION_SECTION_TITLE,
        fresh._preseason_calibration_markdown(),
        insert_before=[route.NEXT_SECTION],
    )
    return guarded


def apply_guard(local_day=None):
    day = fresh._local_day(local_day)
    if not fresh._preseason_mode(day):
        print("15 Eylül ve sonrası: yasak dönemi son rota koruması devre dışı.")
        return False

    payload = json.loads(route.REPORT_JSON.read_text(encoding="utf-8"))
    candidates = payload.get("saha_adaylari", [])
    shortlist = fresh.select_fresh_shortlist(candidates, local_day=day)

    original_json = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    payload["gunun_ilk_3_kontrolu"] = shortlist
    payload["gunun_ilk_3_notu"] = fresh.PRESEASON_NOTE
    payload["kuru_zemin_kalibrasyon_kontrolu"] = []
    payload["kuru_zemin_kalibrasyon_notu"] = fresh.PRESEASON_CALIBRATION_NOTE
    payload["preseason_route_final_guard"] = {
        "durum": "uygulandi",
        "tarih": day.isoformat(),
        "kural": (
            "15 Eylul 2026 oncesi eski/gecikmis backlog ilk-3 saha rotasina geri "
            "yazilamaz; yalniz taze guclu Sentinel kaniti veya insan TEKRAR talebi gecer."
        ),
    }
    rendered_json = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    json_changed = rendered_json != original_json
    if json_changed:
        route.REPORT_JSON.write_text(rendered_json, encoding="utf-8")

    markdown_changed = False
    if route.FIELD_REPORT_MD.exists():
        original_md = route.FIELD_REPORT_MD.read_text(encoding="utf-8")
        guarded_md = _apply_markdown_guard(original_md, shortlist)
        if guarded_md != original_md:
            route.FIELD_REPORT_MD.write_text(guarded_md, encoding="utf-8")
            markdown_changed = True

    print(
        "Yasak dönemi son rota koruması: "
        f"ilk3={len(shortlist)}, json_değişti={json_changed}, markdown_değişti={markdown_changed}"
    )
    return json_changed or markdown_changed


def _self_check():
    west = route.SATELLITE_REGION_LABELS[0]
    old = {
        "gorev_id": "OLD",
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "GECİKEN",
        "mahalle": "Dalyan",
        "enlem": 38.33,
        "boylam": 26.31,
        "alan_m2": 400,
        "bolge": west,
        "boyut_sinifi": "KUCUK",
        "uydu_onceligi": "ORTA",
        "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
        "yeni_goruntu": False,
    }
    fresh_small = dict(old)
    fresh_small.update(
        {
            "gorev_id": "FRESH",
            "mahalle": "Ovacık",
            "yeni_goruntu": True,
            "oncelik": "ORTA",
        }
    )
    repeat = dict(old)
    repeat.update(
        {
            "gorev_id": "REPEAT",
            "mahalle": "Şifne",
            "saha_durumu": "TEKRAR_GIT",
            "oncelik": "TEKRAR",
        }
    )
    chosen = fresh.select_fresh_shortlist(
        [old, fresh_small, repeat],
        local_day=date(2026, 9, 2),
    )
    chosen_ids = {item.get("gorev_id") for item in chosen}
    assert "OLD" not in chosen_ids, chosen
    assert {"FRESH", "REPEAT"}.issubset(chosen_ids), chosen

    sample_md = "\n".join(
        [
            "# Rapor",
            "",
            route.SECTION_TITLE,
            "",
            "1. ESKI GECIKMIS",
            "",
            route.CALIBRATION_SECTION_TITLE,
            "",
            "1. ESKI KALIBRASYON",
            "",
            "## Aday tavanı şantiye ölçeği devriyesi",
            "",
            "ARKA PLAN KAPASITE",
            "",
            "## Kör alan saha devriyesi",
            "",
            "ARKA PLAN KOR",
            "",
            route.NEXT_SECTION,
            "",
            "ANA LISTE",
            "",
        ]
    )
    guarded = _apply_markdown_guard(sample_md, [])
    assert "ESKI GECIKMIS" not in guarded
    assert "ESKI KALIBRASYON" not in guarded
    assert PRESEASON_EMPTY_MESSAGE in guarded
    assert "ARKA PLAN KAPASITE" in guarded
    assert "ARKA PLAN KOR" in guarded
    assert "ANA LISTE" in guarded
    assert guarded.count(route.SECTION_TITLE) == 1
    assert guarded.count(route.CALIBRATION_SECTION_TITLE) == 1
    assert fresh._preseason_mode(date(2026, 9, 14))
    assert not fresh._preseason_mode(date(2026, 9, 15))
    print("preseason_route_final_guard self-check: OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        return
    apply_guard()


if __name__ == "__main__":
    main()
