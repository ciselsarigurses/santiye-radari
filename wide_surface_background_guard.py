"""Geniş ve düşük-kompaktlık Sentinel hareketlerini operasyon listesinden ayırır.

Bu katman algılama yapmaz, Sentinel eşiklerini değiştirmez ve ham radar adaylarını
silmez. ``gunluk_uydu_raporlari.hareket_json`` içinde zaten ölçülmüş
``genis_geometri_riski`` + ``genis_kompaktlik`` kanıtını kullanır. 10.000 m² üstünde
ve düşük-kompaktlık riski doğrulanmış geniş hareketler tarla/toprak temizliği,
doğal/kırsal yüzey değişimi veya başka geniş arazi müdahaleleri olabileceği için
satış/saha operasyon listesinden ayrılır; ayrı bir arka-plan diagnostik listesinde
koordinatlarıyla korunur.

İnsan tarafından ``TEKRAR_GIT`` denmiş bir kayıt bu katman tarafından gizlenmez.
Kural yalnız rapor/UI sunumunu etkiler; SQLite radar hafızası ve 250 m² ana alarm
eşiği aynen kalır.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "santiye.db"
REPORT_JSON = ROOT / "latest_report.json"
REPORT_MD = ROOT / "SAHA_RAPORU.md"
REVIEW_JSON = ROOT / "wide_surface_background_review.json"

BACKGROUND_MIN_M2 = 10_000
LOW_COMPACTNESS_MAX = 0.15
REPORT_MATCH_METERS = 90
DEDUP_METERS = 90
MIN_AREA_SIMILARITY = 0.50
SECTION_TITLE = "## Arka planda izlenen geniş yüzey hareketleri"
OPERATIONAL_SECTIONS = {
    "## Günün ilk 3 kontrolü",
    "## Bugün sahada kontrol edilecek uydu adayları",
}


def _number(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _point(item):
    if not isinstance(item, dict):
        return None
    lat = _number(item.get("enlem"))
    lon = _number(item.get("boylam"))
    if lat is None or lon is None:
        return None
    return lat, lon


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _area_similarity(first, second):
    a = max(_number(first, 0.0) or 0.0, 0.0)
    b = max(_number(second, 0.0) or 0.0, 0.0)
    if a <= 0 or b <= 0:
        return 0.0
    return min(a, b) / max(a, b)


def is_background_candidate(item):
    """Yalnız ölçülmüş geniş + düşük-kompaktlık riskini arka plana ayır."""
    if not isinstance(item, dict):
        return False
    area = max(_number(item.get("alan_m2"), 0.0) or 0.0, 0.0)
    compactness = _number(item.get("genis_kompaktlik"))
    risk = item.get("genis_geometri_riski") is True
    return bool(
        area > BACKGROUND_MIN_M2
        and risk
        and compactness is not None
        and compactness <= LOW_COMPACTNESS_MAX
    )


def _candidate_payload(raw, region_key, region_label):
    point = _point(raw)
    if point is None:
        return None
    latitude, longitude = point
    area = round(max(_number(raw.get("alan_m2"), 0.0) or 0.0, 0.0))
    compactness = _number(raw.get("genis_kompaktlik"), 0.0) or 0.0
    return {
        "alarm": False,
        "saha_gorevi": False,
        "izleme": "ARKA_PLAN_GENIS_YUZEY",
        "bolge_anahtari": str(region_key or ""),
        "bolge": str(region_label or region_key or "Uydu bölgesi"),
        "mahalle": str(raw.get("mahalle") or "Mevki doğrulanmadı"),
        "enlem": round(latitude, 6),
        "boylam": round(longitude, 6),
        "alan_m2": area,
        "genis_kompaktlik": round(compactness, 3),
        "genis_geometri_riski": True,
        "sinyal": str(raw.get("sinyal") or "Geniş yüzey/toprak değişimi"),
        "harita": (
            "https://www.google.com/maps/dir/?api=1&destination="
            f"{latitude:.6f},{longitude:.6f}"
        ),
        "parsel_on_kontrol": (
            "https://parselsorgu.tkgm.gov.tr/#ara/cografi/"
            f"{latitude:.6f}/{longitude:.6f}"
        ),
        "neden": (
            "10.000 m² üstünde ve ölçülen düşük-kompaktlık riski var. Bu geometri "
            "tek başına şantiye kanıtı sayılmaz; geniş arazi/tarım/toprak/doğal yüzey "
            "değişimi olasılığı nedeniyle arka planda izlenir. Yeni sahnede daha "
            "kompakt-parsel ölçekli veya devam eden güçlü müdahale kanıtı oluşursa "
            "operasyonel havuza yeniden girebilir."
        ),
        "kaynak_bolgeler": [str(region_key or "")],
    }


def _dedupe_candidates(candidates):
    selected = []
    for candidate in sorted(
        candidates,
        key=lambda item: (-int(item.get("alan_m2") or 0), item.get("enlem", 0)),
    ):
        point = _point(candidate)
        if point is None:
            continue
        duplicate = None
        for existing in selected:
            if (
                _distance_m(point, _point(existing)) <= DEDUP_METERS
                and _area_similarity(
                    candidate.get("alan_m2"), existing.get("alan_m2")
                ) >= MIN_AREA_SIMILARITY
            ):
                duplicate = existing
                break
        if duplicate is None:
            selected.append(dict(candidate))
            continue
        for region in candidate.get("kaynak_bolgeler") or []:
            if region and region not in duplicate["kaynak_bolgeler"]:
                duplicate["kaynak_bolgeler"].append(region)
    return selected


def load_background_candidates(report_date):
    if not DB_PATH.exists():
        return []
    candidates = []
    with sqlite3.connect(DB_PATH) as connection:
        rows = connection.execute(
            """SELECT bolge,bolge_adi,hareket_json,hata
            FROM gunluk_uydu_raporlari
            WHERE rapor_tarihi=? ORDER BY bolge""",
            (str(report_date),),
        ).fetchall()
    for region_key, region_label, movement_json, error in rows:
        if error:
            continue
        try:
            movement = json.loads(movement_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(movement, list):
            continue
        for raw in movement:
            if not is_background_candidate(raw):
                continue
            payload = _candidate_payload(raw, region_key, region_label)
            if payload:
                candidates.append(payload)
    return _dedupe_candidates(candidates)


def _manual_repeat(item):
    return (
        str(item.get("oncelik") or "").strip().upper() == "TEKRAR"
        or str(item.get("saha_durumu") or "").strip().upper() == "TEKRAR_GIT"
    )


def report_item_matches_background(item, backgrounds):
    if not isinstance(item, dict) or _manual_repeat(item):
        return False
    area = max(_number(item.get("alan_m2"), 0.0) or 0.0, 0.0)
    if area <= BACKGROUND_MIN_M2:
        return False
    point = _point(item)
    if point is None:
        return False
    for background in backgrounds:
        background_point = _point(background)
        if background_point is None:
            continue
        if _distance_m(point, background_point) > REPORT_MATCH_METERS:
            continue
        if _area_similarity(area, background.get("alan_m2")) < MIN_AREA_SIMILARITY:
            continue
        return True
    return False


def _summary_after_filter(summary, operational, backgrounds):
    text = str(summary or "")
    text = re.sub(
        r" · Aktif saha görevi: \d+",
        f" · Aktif saha görevi: {len(operational)}",
        text,
    )
    overdue = sum(bool(item.get("gecikmis")) for item in operational)
    if overdue:
        if re.search(r" · Geciken kontrol: \d+", text):
            text = re.sub(
                r" · Geciken kontrol: \d+",
                f" · Geciken kontrol: {overdue}",
                text,
            )
    else:
        text = re.sub(r" · Geciken kontrol: \d+", "", text)
    text = re.sub(r" · Arka plan geniş yüzey: \d+", "", text)
    if backgrounds:
        text += f" · Arka plan geniş yüzey: {len(backgrounds)}"
    return text


def annotate_json(backgrounds):
    if not REPORT_JSON.exists():
        return None, [], []
    try:
        payload = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None, [], []
    if not isinstance(payload, dict):
        return None, [], []

    raw_items = payload.get("saha_adaylari") or []
    operational = []
    separated = []
    manual_overrides = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        if _manual_repeat(raw) and report_item_matches_background(
            {**raw, "oncelik": "NORMAL", "saha_durumu": "KONTROLE_GIT"},
            backgrounds,
        ):
            manual_overrides.append(dict(raw))
            operational.append(dict(raw))
        elif report_item_matches_background(raw, backgrounds):
            separated.append(dict(raw))
        else:
            operational.append(dict(raw))

    payload["saha_adaylari"] = operational
    payload["arka_plan_genis_yuzey_hareketleri"] = backgrounds
    payload["arka_plan_kurali"] = {
        "alarm": False,
        "saha_gorevi": False,
        "min_alan_m2": BACKGROUND_MIN_M2,
        "dusuk_kompaktlik_max": LOW_COMPACTNESS_MAX,
        "manuel_tekrar_override": True,
        "aciklama": (
            "Yalnız 10.000 m² üstü + ölçülmüş düşük-kompaktlık riski birlikteyse "
            "operasyon listesinden ayrılır. Ham Sentinel/radar kaydı silinmez."
        ),
    }
    payload["ozet"] = _summary_after_filter(
        payload.get("ozet"), operational, backgrounds
    )
    REPORT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload, separated, manual_overrides


def _strip_existing_background_section(lines):
    output = []
    skipping = False
    for line in lines:
        if line.strip() == SECTION_TITLE:
            skipping = True
            continue
        if skipping and line.startswith("## "):
            skipping = False
        if not skipping:
            output.append(line)
    while output and not output[-1].strip():
        output.pop()
    return output


def _coord_from_block(block):
    pattern = re.compile(
        r"\*\*Koordinat:\*\*\s*`\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*`"
    )
    for line in block:
        match = pattern.search(line)
        if match:
            return float(match.group(1)), float(match.group(2))
    return None


def _markdown_block_matches(block, backgrounds):
    point = _coord_from_block(block)
    if point is None:
        return False
    area = None
    for line in block:
        if "**Değişim alanı:**" in line:
            match = re.search(r"([\d\.]+)\s*m²", line)
            if match:
                try:
                    area = float(match.group(1).replace(".", ""))
                except ValueError:
                    area = None
            break
    if area is not None and area <= BACKGROUND_MIN_M2:
        return False
    for background in backgrounds:
        if _distance_m(point, _point(background)) <= REPORT_MATCH_METERS:
            if area is None or _area_similarity(
                area, background.get("alan_m2")
            ) >= MIN_AREA_SIMILARITY:
                return True
    return False


def _filter_operational_markdown(lines, backgrounds):
    result = []
    section = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("## "):
            section = line.strip()
            result.append(line)
            index += 1
            continue
        if line.startswith("### ") and section in OPERATIONAL_SECTIONS:
            end = index + 1
            while end < len(lines):
                if lines[end].startswith("### ") or lines[end].startswith("## "):
                    break
                end += 1
            block = lines[index:end]
            if _markdown_block_matches(block, backgrounds):
                index = end
                continue
            result.extend(block)
            index = end
            continue
        result.append(line)
        index += 1
    return result


def annotate_markdown(payload, backgrounds):
    if not REPORT_MD.exists():
        return
    lines = REPORT_MD.read_text(encoding="utf-8").splitlines()
    lines = _strip_existing_background_section(lines)
    lines = _filter_operational_markdown(lines, backgrounds)

    if payload:
        summary = str(payload.get("ozet") or "")
        for index, line in enumerate(lines):
            if line.startswith("**Özet:**"):
                lines[index] = f"**Özet:** {summary}"
                break

    while lines and not lines[-1].strip():
        lines.pop()
    lines.extend(["", SECTION_TITLE, ""])
    if not backgrounds:
        lines.extend([
            "Bu raporda ölçülmüş düşük-kompaktlık kuralına giren geniş yüzey hareketi yok.",
            "",
        ])
    else:
        lines.extend([
            "> Bu bölüm **şantiye alarmı veya saha görevi değildir**. 10.000 m² üstü "
            "ve ölçülen düşük-kompaktlık riski taşıyan geniş değişimler; tarım, toprak "
            "temizliği, doğal/kırsal yüzey hareketi veya başka geniş arazi müdahalesi "
            "olasılığı nedeniyle operasyon listesinden ayrılır. Ham radar kaydı silinmez; "
            "yeni görüntüde kompakt/parsel ölçekli veya devam eden güçlü müdahale kanıtı "
            "oluşursa yeniden değerlendirilir.",
            "",
        ])
        for item in backgrounds:
            area_text = f"{int(item['alan_m2']):,}".replace(",", ".")
            lines.extend([
                f"- **{item['mahalle']} · yaklaşık {area_text} m²** — "
                f"kompaktlık {item['genis_kompaktlik']:.3f}; "
                f"koordinat `{item['enlem']}, {item['boylam']}` · "
                f"[Harita]({item['harita']}) · "
                f"[Parsel ön kontrol]({item['parsel_on_kontrol']})",
            ])
        lines.append("")
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


def write_review(report_date, backgrounds, separated, manual_overrides):
    review = {
        "rapor_tarihi": str(report_date or ""),
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": 250,
        "kural": {
            "min_alan_m2": BACKGROUND_MIN_M2,
            "genis_geometri_riski": True,
            "dusuk_kompaktlik_max": LOW_COMPACTNESS_MAX,
            "rapor_esleme_m": REPORT_MATCH_METERS,
            "manuel_tekrar_override": True,
        },
        "arka_plan_aday_sayisi": len(backgrounds),
        "operasyonel_listeden_ayrilan": len(separated),
        "manuel_tekrar_korunan": len(manual_overrides),
        "adaylar": backgrounds,
    }
    REVIEW_JSON.write_text(
        json.dumps(review, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return review


def _self_check():
    background = {
        "enlem": 38.30,
        "boylam": 26.46,
        "alan_m2": 120_000,
        "genis_kompaktlik": 0.08,
        "genis_geometri_riski": True,
    }
    assert is_background_candidate(background)
    assert not is_background_candidate({**background, "genis_geometri_riski": False})
    assert not is_background_candidate({**background, "alan_m2": 9_000})
    assert not is_background_candidate({**background, "genis_kompaktlik": 0.22})

    decorated = _candidate_payload(background, "uzunkuyu", "Uzunkuyu")
    assert decorated is not None
    small_report = {"enlem": 38.30, "boylam": 26.46, "alan_m2": 600}
    assert not report_item_matches_background(small_report, [decorated])
    wide_report = {"enlem": 38.30, "boylam": 26.46, "alan_m2": 118_000}
    assert report_item_matches_background(wide_report, [decorated])
    assert not report_item_matches_background(
        {**wide_report, "saha_durumu": "TEKRAR_GIT"}, [decorated]
    )

    sample = [
        "## Bugün sahada kontrol edilecek uydu adayları",
        "",
        "### 1. YÜKSEK — Test",
        "- **Koordinat:** `38.3, 26.46`",
        "- **Değişim alanı:** yaklaşık 120.000 m²",
        "",
        "### 2. ORTA — Küçük",
        "- **Koordinat:** `38.31, 26.47`",
        "- **Değişim alanı:** yaklaşık 600 m²",
        "",
    ]
    filtered = _filter_operational_markdown(sample, [decorated])
    text = "\n".join(filtered)
    assert "120.000 m²" not in text
    assert "600 m²" in text


def main(check_only=False):
    _self_check()
    if check_only:
        print("Geniş yüzey arka-plan koruması öz testi başarılı.")
        return
    if not REPORT_JSON.exists():
        print("latest_report.json yok; arka-plan katmanı değişiklik yapmadı.")
        return
    try:
        current = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        current = {}
    report_date = current.get("rapor_tarihi") if isinstance(current, dict) else None
    if not report_date:
        print("Rapor tarihi bulunamadı; arka-plan katmanı değişiklik yapmadı.")
        return

    backgrounds = load_background_candidates(report_date)
    payload, separated, manual_overrides = annotate_json(backgrounds)
    annotate_markdown(payload, backgrounds)
    review = write_review(report_date, backgrounds, separated, manual_overrides)
    print(
        "Geniş yüzey arka-plan koruması: "
        f"{review['arka_plan_aday_sayisi']} diagnostik geniş hareket; "
        f"{review['operasyonel_listeden_ayrilan']} operasyon kaydı ayrıldı; "
        f"{review['manuel_tekrar_korunan']} manuel tekrar korundu."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    main(check_only=args.check_only)
