"""Aktif saha görevlerinden günün ilk üç kontrolünü raporda görünür kılar.

Bu katman yeni alarm üretmez, görev açmaz/kapatmaz ve Sentinel eşiklerini değiştirmez.
``report_quality.py`` ve kalıcı uydu metadata hidrasyonu sonrasında oluşan sıralı aktif
saha listesinin ilk üç eyleme dönük kaydını ayrı bir yönetim kısa listesi olarak yazar.
Böylece onlarca açık görev varken sabah saha ekibine verilecek ilk işler net kalır.
"""

from __future__ import annotations

import json
from pathlib import Path


REPORT_JSON = Path(__file__).with_name("latest_report.json")
FIELD_REPORT_MD = Path(__file__).with_name("SAHA_RAPORU.md")
SHORTLIST_LIMIT = 3
ACTIVE_STATUSES = {"KONTROLE_GIT", "TEKRAR_GIT"}
SECTION_TITLE = "## Günün ilk 3 kontrolü"
NEXT_SECTION = "## Bugün sahada kontrol edilecek uydu adayları"


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def select_shortlist(candidates, limit=SHORTLIST_LIMIT):
    """Mevcut kalite sırasını bozmadan ilk eyleme dönük benzersiz görevleri seç."""
    selected = []
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
        item["gunluk_sira"] = len(selected) + 1
        selected.append(item)
        seen.add(task_id)
        if len(selected) >= max(int(limit), 0):
            break
    return selected


def _shortlist_markdown(shortlist):
    lines = [
        SECTION_TITLE,
        "",
        "> Bu bölüm yeni alarm üretmez; aktif saha listesinin mevcut kalite sırasındaki ilk üç görevidir.",
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
        route_text = f" · [Yol tarifi]({route})" if route.startswith(("http://", "https://")) else ""
        lines.append(
            f"{order}. **{priority} — {neighborhood}**{area_text} · Görev `{task_id}`{route_text}"
        )
    lines.append("")
    return "\n".join(lines)


def _inject_markdown(text, section):
    """Kısa liste bölümünü idempotent biçimde ana saha listesinden önce yerleştir."""
    text = str(text or "")
    start = text.find(SECTION_TITLE)
    next_index = text.find(NEXT_SECTION)
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
    payload["gunun_ilk_3_kontrolu"] = shortlist
    payload["gunun_ilk_3_notu"] = (
        "Yeni alarm üretmez; aktif saha görevlerinin mevcut kalite sırasındaki ilk üç kaydıdır."
    )
    REPORT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if FIELD_REPORT_MD.exists():
        current = FIELD_REPORT_MD.read_text(encoding="utf-8")
        updated = _inject_markdown(current, _shortlist_markdown(shortlist))
        FIELD_REPORT_MD.write_text(updated, encoding="utf-8")
    return shortlist


def _self_check():
    sample = [
        {
            "gorev_id": "U1", "saha_durumu": "KONTROLE_GIT", "oncelik": "ERKEN",
            "mahalle": "Alaçatı", "enlem": 38.2, "boylam": 26.4, "alan_m2": 600,
        },
        {
            "gorev_id": "U2", "saha_durumu": "KONTROL_EDILDI", "oncelik": "ERKEN",
            "mahalle": "Ovacık", "enlem": 38.3, "boylam": 26.3, "alan_m2": 500,
        },
        {
            "gorev_id": "U3", "saha_durumu": "TEKRAR_GIT", "oncelik": "TEKRAR",
            "mahalle": "Şifne", "enlem": 38.34, "boylam": 26.39, "alan_m2": 400,
        },
        {
            "gorev_id": "U4", "saha_durumu": "KONTROLE_GIT", "oncelik": "PARSEL",
            "mahalle": "Ildır", "enlem": 38.42, "boylam": 26.57, "alan_m2": 900,
        },
        {
            "gorev_id": "U5", "saha_durumu": "KONTROLE_GIT", "oncelik": "NORMAL",
            "mahalle": "Dalyan", "enlem": 38.32, "boylam": 26.30, "alan_m2": 1200,
        },
    ]
    chosen = select_shortlist(sample)
    assert [item["gorev_id"] for item in chosen] == ["U1", "U3", "U4"]
    assert [item["gunluk_sira"] for item in chosen] == [1, 2, 3]

    base = "# Rapor\n\n" + NEXT_SECTION + "\n\nAdaylar\n"
    once = _inject_markdown(base, _shortlist_markdown(chosen))
    twice = _inject_markdown(once, _shortlist_markdown(chosen))
    assert once == twice, "Kısa liste bölümü tekrar çalıştırmada çoğalmamalı."
    assert once.count(SECTION_TITLE) == 1
    assert once.find(SECTION_TITLE) < once.find(NEXT_SECTION)


if __name__ == "__main__":
    _self_check()
    shortlist = update_daily_shortlist()
    print(
        "Günün ilk kontrol kısa listesi güncellendi: "
        + (", ".join(str(item.get("gorev_id")) for item in shortlist) or "aktif görev yok")
    )
