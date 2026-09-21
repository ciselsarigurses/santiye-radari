"""Genel Sentinel zemin değişimini temel/kepçe çoklu kanıtı olmadan operasyona çıkarmaz.

Bu katman alarm veya saha görevi üretmez, SQLite görev durumunu değiştirmez ve 250 m²
ana / 150–249 m² MİKRO eşiklerine dokunmaz. Yalnız kullanıcıya sunulan nihai operasyon
rotasını korur: ``KONTROLE_GIT`` durumundaki genel yüzey/toprak değişimi adayları,
aynı Sentinel-2 sahnesinde ``foundation_excavation_positive_context_review.json``
içinde 250 m²+ korunmuş çoklu-pozitif temel/kepçe adayıyla mekânsal olarak eşleşmedikçe
rota dışı diagnostik arka plana alınır. İnsan tarafından açıkça istenen ``TEKRAR_GIT``
kayıtları korunur.

Amaç BSI/toprak değişiminin tek başına saha rotası üretmesini engellerken aday kaydını
ve geri alınabilirliği korumaktır.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path


BASE = Path(__file__).resolve().parent
ROUTE_JSON = BASE / "operational_route.json"
REPORT_JSON = BASE / "latest_report.json"
FIELD_REPORT_MD = BASE / "SAHA_RAPORU.md"
POSITIVE_CONTEXT_JSON = BASE / "foundation_excavation_positive_context_review.json"

MATCH_RADIUS_M = 60.0
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
SECTION_TITLE = "## Günün ilk 3 kontrolü"
NEXT_SECTION = "## Ek kuru zemin kalibrasyon kontrolü"


def _load(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _point(item):
    try:
        return float(item["enlem"]), float(item["boylam"])
    except (KeyError, TypeError, ValueError):
        return None


def _distance_m(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _date(value):
    text = str(value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _generic_surface_candidate(item):
    if str(item.get("saha_durumu") or "").strip().upper() != "KONTROLE_GIT":
        return False
    signal = str(item.get("sinyal") or "").casefold()
    return (
        "yüzey/toprak değişimi" in signal
        or "toprak değişimi" in signal
        or "küçük, güçlü yüzey" in signal
    )


def _supported_main_rows(context):
    rows = []
    for region_key, region in (context.get("bolgeler") or {}).items():
        if not isinstance(region, dict):
            continue
        scene = _date(region.get("pozitif_s2_son_tarih"))
        if not scene:
            continue
        for raw in region.get("adaylar") or []:
            if not isinstance(raw, dict) or _point(raw) is None:
                continue
            if raw.get("ana_esik_pozitif_destekli_diagnostik_korumali") is not True:
                continue
            try:
                area = float(raw.get("spektral_etki_alani_m2") or 0)
            except (TypeError, ValueError):
                area = 0.0
            if area < MAIN_THRESHOLD_M2:
                continue
            rows.append(
                {
                    "region_key": str(region_key),
                    "bolge": str(region.get("bolge") or ""),
                    "scene": scene,
                    "enlem": float(raw["enlem"]),
                    "boylam": float(raw["boylam"]),
                    "alan_m2": area,
                    "morfoloji_puani": raw.get("morfoloji_puani"),
                    "pozitif_kanit_kaynaklari": raw.get("pozitif_kanit_kaynaklari") or [],
                }
            )
    return rows


def _support_for(item, supports):
    point = _point(item)
    scene = _date(item.get("son_tarih"))
    if point is None or not scene:
        return None, None
    item_region = str(item.get("bolge") or "")
    nearest = None
    nearest_distance = None
    for support in supports:
        if support["scene"] != scene:
            continue
        support_region = str(support.get("bolge") or "")
        if item_region and support_region and item_region != support_region:
            continue
        distance = _distance_m(point, (support["enlem"], support["boylam"]))
        if nearest_distance is None or distance < nearest_distance:
            nearest = support
            nearest_distance = distance
    if nearest is None or nearest_distance is None or nearest_distance > MATCH_RADIUS_M:
        return None, nearest_distance
    return nearest, nearest_distance


def guard_route(route, context):
    supports = _supported_main_rows(context)
    source = route.get("operasyonel_rota") or []
    kept = []
    background = []

    for raw in source:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        status = str(item.get("saha_durumu") or "").strip().upper()
        if status == "TEKRAR_GIT" or not _generic_surface_candidate(item):
            kept.append(item)
            continue

        support, distance = _support_for(item, supports)
        if support is not None:
            item["temel_kazi_coklu_kanit_destekli"] = True
            item["temel_kazi_destek_mesafe_m"] = round(float(distance), 1)
            item["temel_kazi_morfoloji_puani"] = support.get("morfoloji_puani")
            item["temel_kazi_pozitif_kanit_kaynaklari"] = support.get("pozitif_kanit_kaynaklari")
            kept.append(item)
            continue

        item["temel_kazi_coklu_kanit_destekli"] = False
        item["temel_kazi_kapisi"] = "GENEL_ZEMIN_DEGISIMI_TEK_BASINA_ROTA_DEGIL"
        item["rota_uygun"] = False
        item["operasyonel_durum"] = "DIAGNOSTIK_ARKA_PLAN"
        background.append(item)

    for index, item in enumerate(kept, start=1):
        item["gunluk_sira"] = index

    before_ids = [str(x.get("gorev_id") or "") for x in source if isinstance(x, dict)]
    after_ids = [str(x.get("gorev_id") or "") for x in kept]
    result = dict(route)
    result["operasyonel_rota"] = kept
    result["operasyon_odak_sonrasi_rota_gorevleri"] = after_ids
    result["temel_kazi_kapisi_sonrasi_rota_gorevleri"] = after_ids
    result["temel_kazi_kapisi_arka_plan"] = background
    result["temel_kazi_kapisi_arka_plan_sayi"] = len(background)
    result["temel_kazi_kapisi_destekli_ana_aday_sayi"] = len(supports)
    result["temel_kazi_kapisi_eslesme_m"] = MATCH_RADIUS_M
    result["temel_kazi_kapisi_degisti"] = before_ids != after_ids
    result["temel_kazi_kapisi_notu"] = (
        "Genel Sentinel yüzey/toprak değişimi KONTROLE_GIT için tek başına yeterli değildir. "
        "Aynı S2 sahnesinde 250 m²+ korunmuş çoklu-pozitif temel/kepçe diagnostik desteği "
        "olmayan kayıtlar silinmeden diagnostik arka plana alınır. TEKRAR_GIT insan kararı korunur."
    )
    return result


def _update_report(report, guarded_route):
    final_route = [dict(x) for x in guarded_route.get("operasyonel_rota") or []]
    report = dict(report)
    report["gunun_ilk_3_kontrolu"] = final_route
    report["gunun_ilk_3_notu"] = guarded_route.get("temel_kazi_kapisi_notu")
    report["temel_kazi_kapisi_arka_plan_sayi"] = guarded_route.get(
        "temel_kazi_kapisi_arka_plan_sayi", 0
    )
    report["temel_kazi_kapisi_aktif"] = True
    return report


def _route_markdown(route):
    lines = [
        SECTION_TITLE,
        "",
        "> **Temel/kepçe çoklu-kanıt kapısı aktif.** Genel BSI/toprak değişimi tek başına saha rotası üretmez; 250 m²+ aynı-sahne temel/kepçe desteği gerekir. TEKRAR_GIT insan kararı korunur.",
        "",
    ]
    if not route:
        lines.extend(["Bugün çoklu-kanıt kapısını geçen operasyonel temel/kepçe noktası yok.", ""])
        return "\n".join(lines)
    for index, item in enumerate(route, start=1):
        neighborhood = str(item.get("mahalle") or "Konum araştırılıyor")
        area = int(float(item.get("alan_m2") or 0))
        area_text = f"{area:,}".replace(",", ".")
        task_id = str(item.get("gorev_id") or "-")
        href = str(item.get("harita") or "")
        link = f" · [Yol tarifi]({href})" if href.startswith(("http://", "https://")) else ""
        lines.append(
            f"{index}. **{str(item.get('oncelik') or 'KONTROL')} — {neighborhood}** · "
            f"yaklaşık {area_text} m² · Görev `{task_id}`{link}"
        )
    lines.append("")
    return "\n".join(lines)


def _update_markdown(text, route):
    text = str(text or "")
    start = text.find(SECTION_TITLE)
    if start < 0:
        return _route_markdown(route) + "\n" + text
    end = text.find(NEXT_SECTION, start)
    if end < 0:
        end = len(text)
    return text[:start].rstrip() + "\n\n" + _route_markdown(route).rstrip() + "\n\n" + text[end:].lstrip()


def _self_check():
    context = {
        "bolgeler": {
            "cesme": {
                "bolge": "Çeşme merkez · Alaçatı · Ilıca",
                "pozitif_s2_son_tarih": "18.09.2026",
                "adaylar": [
                    {
                        "enlem": 38.3001,
                        "boylam": 26.3001,
                        "spektral_etki_alani_m2": 400,
                        "morfoloji_puani": 95,
                        "pozitif_kanit_kaynaklari": ["S2_MORFOLOJI", "S1_LOKAL_POZITIF"],
                        "ana_esik_pozitif_destekli_diagnostik_korumali": True,
                    }
                ],
            }
        }
    }
    base = {
        "bolge": "Çeşme merkez · Alaçatı · Ilıca",
        "son_tarih": "18.09.2026",
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "ERKEN",
        "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
        "alan_m2": 400,
    }
    route = {
        "operasyonel_rota": [
            {**base, "gorev_id": "SUPPORTED", "enlem": 38.3001, "boylam": 26.3001},
            {**base, "gorev_id": "RAW", "enlem": 38.31, "boylam": 26.31},
            {**base, "gorev_id": "REPEAT", "saha_durumu": "TEKRAR_GIT", "enlem": 38.32, "boylam": 26.32},
        ]
    }
    guarded = guard_route(route, context)
    ids = [x["gorev_id"] for x in guarded["operasyonel_rota"]]
    assert ids == ["SUPPORTED", "REPEAT"], guarded
    assert guarded["temel_kazi_kapisi_arka_plan_sayi"] == 1
    assert guarded["temel_kazi_kapisi_arka_plan"][0]["gorev_id"] == "RAW"

    stale = json.loads(json.dumps(context))
    stale["bolgeler"]["cesme"]["pozitif_s2_son_tarih"] = "17.09.2026"
    stale_guarded = guard_route({"operasyonel_rota": [{**base, "gorev_id": "STALE", "enlem": 38.3001, "boylam": 26.3001}]}, stale)
    assert stale_guarded["operasyonel_rota"] == []

    micro = json.loads(json.dumps(context))
    micro_row = micro["bolgeler"]["cesme"]["adaylar"][0]
    micro_row["spektral_etki_alani_m2"] = 200
    micro_row["ana_esik_pozitif_destekli_diagnostik_korumali"] = False
    micro_guarded = guard_route({"operasyonel_rota": [{**base, "gorev_id": "MICRO", "enlem": 38.3001, "boylam": 26.3001}]}, micro)
    assert micro_guarded["operasyonel_rota"] == []

    map_url = "https://www.google.com/maps/dir/?api=1&destination=38.307849,26.333503"
    markdown = _route_markdown([
        {
            "oncelik": "YUKSEK",
            "mahalle": None,
            "alan_m2": 1200,
            "gorev_id": "MAP",
            "harita": map_url,
        }
    ])
    assert "yaklaşık 1.200 m²" in markdown, markdown
    assert map_url in markdown, markdown
    assert "destination=38.307849.26.333503" not in markdown, markdown


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("foundation excavation operational route guard self-check: ok")
        return

    route = _load(ROUTE_JSON)
    context = _load(POSITIVE_CONTEXT_JSON)
    guarded = guard_route(route, context)
    ROUTE_JSON.write_text(json.dumps(guarded, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = _update_report(_load(REPORT_JSON), guarded)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if FIELD_REPORT_MD.exists():
        text = FIELD_REPORT_MD.read_text(encoding="utf-8")
        FIELD_REPORT_MD.write_text(
            _update_markdown(text, guarded.get("operasyonel_rota") or []),
            encoding="utf-8",
        )

    print(
        "foundation operational route guard: "
        f"rota={len(guarded.get('operasyonel_rota') or [])} · "
        f"arka_plan={guarded.get('temel_kazi_kapisi_arka_plan_sayi', 0)} · "
        f"destekli_ana={guarded.get('temel_kazi_kapisi_destekli_ana_aday_sayi', 0)}"
    )


if __name__ == "__main__":
    main()
