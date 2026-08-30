"""Aktif saha görevlerinden günün ilk üç kontrolünü raporda görünür kılar.

Bu katman yeni alarm üretmez, görev açmaz/kapatmaz ve Sentinel eşiklerini değiştirmez.
``report_quality.py`` ve kalıcı uydu metadata hidrasyonu sonrasında oluşan sıralı aktif
saha listesinin ilk üç eyleme dönük kaydını ayrı bir yönetim kısa listesi olarak yazar.
Mevcut kalite sırası mümkün olduğunca korunur; hem Çeşme hem Uzunkuyu tarafında aktif
uydu görevi varsa üç kişilik listede en az bir yer doğu-batı kapsama dengesi için
karşı bölgeye ayrılır. Böylece onlarca açık görev varken sabah saha ekibine verilecek
ilk işler net kalır ve tek analiz kutusunda yığılma nedeniyle diğer yarımada tarafı
operasyonel olarak kör kalmaz.
"""

from __future__ import annotations

import json
from pathlib import Path


REPORT_JSON = Path(__file__).with_name("latest_report.json")
FIELD_REPORT_MD = Path(__file__).with_name("SAHA_RAPORU.md")
SHORTLIST_LIMIT = 3
ACTIVE_STATUSES = {"KONTROLE_GIT", "TEKRAR_GIT"}
SATELLITE_REGION_LABELS = (
    "Çeşme merkez · Alaçatı · Ilıca",
    "Uzunkuyu · Germiyan · Ildır",
)
SECTION_TITLE = "## Günün ilk 3 kontrolü"
NEXT_SECTION = "## Bugün sahada kontrol edilecek uydu adayları"


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


def _balance_satellite_regions(eligible, selected, limit):
    """Kalite sırasını bozmadan eksik uydu bölgesine son listede tek yer aç."""
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
        for index in range(len(selected) - 1, -1, -1):
            region = str(selected[index].get("bolge") or "")
            if region in region_counts and region_counts.get(region, 0) > 1:
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
    """İlk kalite sırasını koru; mümkünse Çeşme ve Uzunkuyu'dan temsil bırak."""
    cap = max(int(limit), 0)
    if cap <= 0:
        return []

    eligible = _actionable_candidates(candidates)
    selected = [dict(item) for item in eligible[:cap]]
    selected = _balance_satellite_regions(eligible, selected, cap)
    for index, item in enumerate(selected, start=1):
        item["gunluk_sira"] = index
    return selected


def _shortlist_markdown(shortlist):
    lines = [
        SECTION_TITLE,
        "",
        "> Bu bölüm yeni alarm üretmez; mevcut kalite sırasını mümkün olduğunca korur ve iki uydu bölgesinde de aktif iş varsa doğu-batı kapsamasını üç kontrol içinde dengeler.",
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
        "Yeni alarm üretmez; kalite sırasını mümkün olduğunca korur ve iki uydu bölgesinde aktif görev varsa üç kontrolde doğu-batı temsili bırakır."
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
    west = SATELLITE_REGION_LABELS[0]
    east = SATELLITE_REGION_LABELS[1]
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
    assert [item["gorev_id"] for item in chosen] == ["U1", "U3", "U5"]
    assert [item["gunluk_sira"] for item in chosen] == [1, 2, 3]
    assert {item["bolge"] for item in chosen} == {west, east}

    one_region = select_shortlist(sample[:4])
    assert [item["gorev_id"] for item in one_region] == ["U1", "U3", "U4"]

    short_limit = select_shortlist(sample, limit=1)
    assert [item["gorev_id"] for item in short_limit] == ["U1"]

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
