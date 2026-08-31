"""Kalıcı Sentinel kör alanlarından günlük insan devriyesi için en fazla iki nokta seçer.

Bu katman şantiye alarmı veya saha görevi üretmez. Amaç, Sentinel zaman serisinde
kara olduğu bilindiği halde bulut/gölge/geçersizlik yüzünden gözlemsiz kalan küçük
alanları her gün rotasyonlu biçimde insan gözüyle kapatmaktır. Böylece "her sokakta
bir personel varmış gibi" kapsama hedefinde uydu körlüğü sessizce saklanmaz.
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path


AUDIT_JSON = Path(__file__).with_name("coverage_blind_area_audit.json")
REPORT_JSON = Path(__file__).with_name("latest_report.json")
FIELD_REPORT_MD = Path(__file__).with_name("SAHA_RAPORU.md")

SECTION_TITLE = "## Kör alan saha devriyesi"
NEXT_SECTION = "## Bugün sahada kontrol edilecek uydu adayları"
ACTIVE_STATUSES = {"KONTROLE_GIT", "TEKRAR_GIT"}
MIN_AREA_M2 = 250
TARGET_MAX_AREA_M2 = 6500
ACTIVE_DISTANCE_M = 150
CROSS_REGION_DISTANCE_M = 250
MAX_PER_REGION = 1
TOTAL_LIMIT = 2


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


def _active_points(report_payload):
    points = []
    for item in (report_payload or {}).get("saha_adaylari") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("saha_durumu") or "").strip().upper()
        if status not in ACTIVE_STATUSES:
            continue
        point = _point(item)
        if point is not None:
            points.append(point)
    return points


def _far_enough(point, points, minimum_m):
    return all(_distance_m(point, old) >= minimum_m for old in points)


def _rotation_offset(report_payload, size):
    if size <= 1:
        return 0
    raw = str((report_payload or {}).get("rapor_tarihi") or "")
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return 0
    return day.toordinal() % size


def _candidate_pool(region_key, region_data):
    if not isinstance(region_data, dict) or region_data.get("durum") != "ok":
        return []

    rows = []
    for raw in region_data.get("cozumlenmemis_yuzey_ornekleri") or []:
        if not isinstance(raw, dict):
            continue
        point = _point({"enlem": raw.get("enlem"), "boylam": raw.get("boylam")})
        area = _number(raw.get("alan_m2"), 0)
        if point is None or area < MIN_AREA_M2:
            continue
        item = {
            "bolge_anahtari": str(region_key),
            "bolge": str(region_data.get("bolge") or region_key),
            "mahalle": str(raw.get("mahalle_yaklasik") or "Yakın bölge"),
            "enlem": round(point[0], 6),
            "boylam": round(point[1], 6),
            "alan_m2": int(round(area)),
            "neden": str(raw.get("neden") or "GOZLEM_YOK"),
            "referans_sahne_sayisi": int(_number(region_data.get("kara_referans_sahne_sayisi"), 0)),
            "kalan_kor_yuzde": _number(region_data.get("kalan_kor_yuzde"), 0),
            "alarm": False,
            "durum": "KAPSAMA_KONTROLU",
        }
        item["harita"] = (
            "https://www.google.com/maps/dir/?api=1&destination="
            f"{item['enlem']:.6f},{item['boylam']:.6f}"
        )
        rows.append(item)

    # Önce şantiye/parsel ölçeğine daha yakın kör kümeleri, sonra daha genişleri kullan.
    rows.sort(
        key=lambda item: (
            0 if item["alan_m2"] <= TARGET_MAX_AREA_M2 else 1,
            0 if item["neden"] == "BULUT_GOLGE_KALICI" else 1,
            item["alan_m2"] if item["alan_m2"] <= TARGET_MAX_AREA_M2 else -item["alan_m2"],
            item["mahalle"],
        )
    )
    return rows


def select_coverage_patrol(audit_payload, report_payload, limit=TOTAL_LIMIT):
    cap = max(int(limit), 0)
    if cap <= 0 or not isinstance(audit_payload, dict):
        return []

    regions = audit_payload.get("bolgeler") or {}
    if not isinstance(regions, dict):
        return []

    active_points = _active_points(report_payload)
    selected = []
    selected_points = []

    for region_key, region_data in regions.items():
        if len(selected) >= cap:
            break
        pool = _candidate_pool(region_key, region_data)
        if not pool:
            continue

        preferred = [item for item in pool if item["alan_m2"] <= TARGET_MAX_AREA_M2]
        fallback = [item for item in pool if item["alan_m2"] > TARGET_MAX_AREA_M2]

        def rotated(items):
            if not items:
                return []
            offset = _rotation_offset(report_payload, len(items))
            return items[offset:] + items[:offset]

        # Günlük rotasyon, parsel/şantiye ölçeği tercih katmanının dışına taşmasın.
        # Ancak o katmanda güvenli nokta kalmazsa daha geniş kör kümeye düş.
        ordered = rotated(preferred) + rotated(fallback)
        picked = None
        for item in ordered:
            point = _point(item)
            if point is None:
                continue
            if not _far_enough(point, active_points, ACTIVE_DISTANCE_M):
                continue
            if not _far_enough(point, selected_points, CROSS_REGION_DISTANCE_M):
                continue
            picked = dict(item)
            break

        if picked is None:
            continue
        selected.append(picked)
        selected_points.append(_point(picked))
        if sum(1 for x in selected if x["bolge_anahtari"] == str(region_key)) >= MAX_PER_REGION:
            continue

    return selected[:cap]


def _markdown(items):
    lines = [
        SECTION_TITLE,
        "",
        "> **Alarm değildir.** Sentinel zaman serisinde kara olduğu bilinen fakat bulut/gölge veya geçersizlik nedeniyle halen gözlemsiz kalan alanlardan günlük en fazla iki nokta seçilir. Aktif radar görevlerinin en az 150 m dışındadır ve iki uydu kutusunda aynı kör alanı iki kez göstermemek için 250 m mekânsal ayrım uygulanır. Aynı görüntü günlerce değişmezse noktalar günlük rotasyonla değişir.",
        "",
    ]
    if not items:
        lines.extend(["Bugün için ek kör alan devriye noktası seçilmedi.", ""])
        return "\n".join(lines)

    for index, item in enumerate(items, start=1):
        area = f"{int(item['alan_m2']):,}".replace(",", ".")
        reason = (
            "kalıcı bulut/gölge"
            if item.get("neden") == "BULUT_GOLGE_KALICI"
            else "karışık geçersizlik"
        )
        refs = int(item.get("referans_sahne_sayisi") or 0)
        lines.append(
            f"{index}. **KÖR ALAN — {item['mahalle']}** · yaklaşık {area} m² · "
            f"{reason} · {refs} açık kara referans sahnesi · "
            f"[Yol tarifi]({item['harita']})"
        )
        lines.append(
            "   - Saha notu: Bu bir şantiye alarmı değildir. Noktaya giderken çevrede yeni hafriyat, kazı, temel veya şantiye kurulumu görülürse fotoğraf ve konumla normal saha kaydı aç."
        )
    lines.append("")
    return "\n".join(lines)


def _inject(text, section):
    text = str(text or "")
    start = text.find(SECTION_TITLE)
    if start >= 0:
        end = text.find(NEXT_SECTION, start)
        if end >= 0:
            text = text[:start].rstrip() + "\n\n" + text[end:]
        else:
            text = text[:start].rstrip() + "\n"

    marker = text.find(NEXT_SECTION)
    if marker < 0:
        return text.rstrip() + "\n\n" + section.rstrip() + "\n"
    return (
        text[:marker].rstrip()
        + "\n\n"
        + section.rstrip()
        + "\n\n"
        + text[marker:].lstrip()
    )


def update_coverage_patrol():
    if not AUDIT_JSON.exists() or not REPORT_JSON.exists():
        return []

    audit = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
    report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    selected = select_coverage_patrol(audit, report)

    report["kor_alan_saha_devriyesi"] = selected
    report["kor_alan_saha_devriyesi_notu"] = (
        "Alarm/görev değildir; kalıcı Sentinel gözlem boşluklarından günlük rotasyonla "
        "en fazla iki insan kontrol noktası seçilir. Aktif radar görevlerinden >=150 m, "
        "birbirinden >=250 m uzakta tutulur."
    )
    REPORT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if FIELD_REPORT_MD.exists():
        current = FIELD_REPORT_MD.read_text(encoding="utf-8")
        FIELD_REPORT_MD.write_text(
            _inject(current, _markdown(selected)),
            encoding="utf-8",
        )
    return selected


def _self_check():
    audit = {
        "bolgeler": {
            "west": {
                "durum": "ok",
                "bolge": "Batı",
                "kara_referans_sahne_sayisi": 8,
                "kalan_kor_yuzde": 0.4,
                "cozumlenmemis_yuzey_ornekleri": [
                    {
                        "mahalle_yaklasik": "Yakın",
                        "enlem": 38.3000,
                        "boylam": 26.3000,
                        "alan_m2": 900,
                        "neden": "BULUT_GOLGE_KALICI",
                    },
                    {
                        "mahalle_yaklasik": "Uzak",
                        "enlem": 38.3100,
                        "boylam": 26.3100,
                        "alan_m2": 700,
                        "neden": "BULUT_GOLGE_KALICI",
                    },
                    {
                        "mahalle_yaklasik": "Geniş",
                        "enlem": 38.3200,
                        "boylam": 26.3200,
                        "alan_m2": 15000,
                        "neden": "KARISIK_GECERSIZLIK",
                    },
                ],
            },
            "east": {
                "durum": "ok",
                "bolge": "Doğu",
                "kara_referans_sahne_sayisi": 8,
                "kalan_kor_yuzde": 0.2,
                "cozumlenmemis_yuzey_ornekleri": [
                    {
                        "mahalle_yaklasik": "Doğu",
                        "enlem": 38.4000,
                        "boylam": 26.6000,
                        "alan_m2": 1200,
                        "neden": "KARISIK_GECERSIZLIK",
                    }
                ],
            },
        }
    }
    report = {
        "rapor_tarihi": "2026-08-31",
        "saha_adaylari": [
            {
                "saha_durumu": "KONTROLE_GIT",
                "enlem": 38.3001,
                "boylam": 26.3001,
            }
        ],
    }
    chosen = select_coverage_patrol(audit, report)
    assert len(chosen) == 2
    assert {item["bolge_anahtari"] for item in chosen} == {"west", "east"}
    assert all(item["alarm"] is False for item in chosen)
    assert all(item["durum"] == "KAPSAMA_KONTROLU" for item in chosen)
    assert not any(item["mahalle"] == "Yakın" for item in chosen)
    west_choice = next(item for item in chosen if item["bolge_anahtari"] == "west")
    assert west_choice["alan_m2"] <= TARGET_MAX_AREA_M2

    md = _inject("Başlık\n\n" + NEXT_SECTION + "\nAday\n", _markdown(chosen))
    assert md.count(SECTION_TITLE) == 1
    md2 = _inject(md, _markdown(chosen))
    assert md2.count(SECTION_TITLE) == 1


if __name__ == "__main__":
    _self_check()
    update_coverage_patrol()
