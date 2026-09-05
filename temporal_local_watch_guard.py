"""250-900 m² güçlü temporal-lokal zemin değişimlerini görünür diagnostik izlemeye alır.

Bu katman ana Sentinel üretim alarmını veya saha görevlerini değiştirmez. Yalnız aynı güncel
temporal_locality_audit.json içinde hem ani başlangıç hem lokal/kompakt karakter gösteren,
çevresi yaygın hareket taşımayan ve veri kalitesi yeterli adayları ayrı bir izleme listesine
koyar. Amaç, özellikle ana çoklu-spektral üretim maskesine girmemiş olabilecek erken hafriyat
sinyallerinin diagnostik dosyalarda saklı kalmamasıdır.

15 Eylül 2026 öncesinde kayıtlar yalnız kalibrasyon/izleme statüsündedir. 15 Eylül ve
sonrasında aynı kanıt "yüksek diagnostik" operasyonel ağırlık etiketi alır; yine de bu script
alarm üretmez veya görev açmaz. Kesin adres, ada/parsel veya hukuki statü türetmez.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date
from pathlib import Path


TEMPORAL_LOCALITY = Path(__file__).with_name("temporal_locality_audit.json")
REPORT_JSON = Path(__file__).with_name("latest_report.json")
FIELD_REPORT_MD = Path(__file__).with_name("SAHA_RAPORU.md")
OUTPUT_JSON = Path(__file__).with_name("temporal_local_watch.json")

MAIN_THRESHOLD_M2 = 250
MAX_DIAGNOSTIC_AREA_M2 = 900
MIN_LOCALITY_RATIO = 1.5
MIN_VALID_RATIO = 2 / 3
MIN_INNER_BSI_CHANGE = 0.10
MIN_ACTIVE_DISTANCE_M = 120
MIN_SELECTED_DISTANCE_M = 45
WATCH_LIMIT = 2
SEASON_START = date(2026, 9, 15)

SECTION_TITLE = "## Temporal-lokal erken sinyal izleme"
INSERT_BEFORE = (
    "## Kör alan saha devriyesi",
    "## Bugün sahada kontrol edilecek uydu adayları",
)
ACTIVE_STATUSES = {"KONTROLE_GIT", "TEKRAR_GIT"}


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _point(item):
    if not isinstance(item, dict):
        return None
    try:
        return float(item.get("enlem")), float(item.get("boylam"))
    except (TypeError, ValueError):
        return None


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _report_day(report_payload):
    try:
        return date.fromisoformat(str(report_payload.get("rapor_tarihi") or ""))
    except (AttributeError, TypeError, ValueError):
        return None


def _active_candidates(report_payload):
    rows = []
    for raw in (report_payload.get("saha_adaylari") or []):
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("saha_durumu") or "KONTROLE_GIT").strip().upper()
        if status not in ACTIVE_STATUSES:
            continue
        if _point(raw) is not None:
            rows.append(raw)
    return rows


def _far_from(items, candidate, minimum_m):
    point = _point(candidate)
    if point is None:
        return False
    for other in items or []:
        other_point = _point(other)
        if other_point is not None and _distance_m(point, other_point) < minimum_m:
            return False
    return True


def _same_report_day(audit_payload, report_payload):
    return str(audit_payload.get("rapor_tarihi") or "") == str(
        report_payload.get("rapor_tarihi") or ""
    )


def _eligible(raw):
    if not isinstance(raw, dict):
        return False
    area = _number(raw.get("alan_m2"), 0)
    if not (MAIN_THRESHOLD_M2 <= area <= MAX_DIAGNOSTIC_AREA_M2):
        return False
    if not bool(raw.get("ani_baslangic_destegi")):
        return False
    if not bool(raw.get("lokal_ani_baslangic_destegi")):
        return False
    if bool(raw.get("yaygin_cevre_degisim_riski")):
        return False
    if _number(raw.get("ic_3x3_gecerli_oran"), 0) < MIN_VALID_RATIO:
        return False
    if _number(raw.get("cevre_halka_gecerli_oran"), 0) < MIN_VALID_RATIO:
        return False
    if _number(raw.get("yerellik_orani"), 0) < MIN_LOCALITY_RATIO:
        return False
    if abs(_number(raw.get("ic_3x3_son_bsi_degisim"), 0)) < MIN_INNER_BSI_CHANGE:
        return False
    return _point(raw) is not None


def select_watch(audit_payload, report_payload, limit=WATCH_LIMIT, local_day=None):
    """Yalnız güncel, güçlü temporal-lokal ve aktif görevden bağımsız adayları seç."""
    cap = max(int(limit), 0)
    if cap <= 0 or not isinstance(audit_payload, dict) or not isinstance(report_payload, dict):
        return []
    if not _same_report_day(audit_payload, report_payload):
        return []

    active = _active_candidates(report_payload)
    day = local_day or _report_day(report_payload) or date.today()
    season_open = day >= SEASON_START

    pool = []
    regions = audit_payload.get("bolgeler") or {}
    if not isinstance(regions, dict):
        return []

    for region_key, region_data in regions.items():
        if not isinstance(region_data, dict) or region_data.get("durum") != "ok":
            continue
        for raw in region_data.get("adaylar") or []:
            if not _eligible(raw):
                continue
            if not _far_from(active, raw, MIN_ACTIVE_DISTANCE_M):
                continue

            item = dict(raw)
            point = _point(item)
            item["enlem"] = round(point[0], 6)
            item["boylam"] = round(point[1], 6)
            item["bolge_anahtari"] = str(region_key)
            item["bolge"] = str(region_data.get("bolge") or region_key)
            item["mahalle"] = str(item.get("mahalle") or "Mevki doğrulanmadı")
            item["onceki_sentinel_item"] = str(region_data.get("onceki_item") or "")
            item["son_sentinel_item"] = str(region_data.get("son_item") or "")
            item["alarm"] = False
            item["saha_gorevi"] = False
            item["operasyonel_agirlik"] = (
                "YUKSEK_DIAGNOSTIK" if season_open else "KALIBRASYON_DIAGNOSTIK"
            )
            item["harita"] = (
                "https://www.google.com/maps/dir/?api=1&destination="
                f"{item['enlem']:.6f},{item['boylam']:.6f}"
            )
            item["parsel_sorgu"] = (
                "https://parselsorgu.tkgm.gov.tr/#ara/cografi/"
                f"{item['enlem']:.6f}/{item['boylam']:.6f}"
            )
            pool.append(item)

    pool.sort(
        key=lambda item: (
            -_number(item.get("yerellik_orani"), 0),
            -abs(_number(item.get("ic_3x3_son_bsi_degisim"), 0)),
            _number(item.get("alan_m2"), 0),
            str(item.get("bolge_anahtari") or ""),
        )
    )

    selected = []
    for item in pool:
        if not _far_from(selected, item, MIN_SELECTED_DISTANCE_M):
            continue
        selected.append(item)
        if len(selected) >= cap:
            break
    return selected


def _payload(audit_payload, report_payload, selected):
    day = _report_day(report_payload)
    season_open = bool(day and day >= SEASON_START)
    return {
        "rapor_tarihi": str(report_payload.get("rapor_tarihi") or ""),
        "kaynak_olusturma": str(audit_payload.get("olusturma") or ""),
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "diagnostik_bant_m2": [MAIN_THRESHOLD_M2, MAX_DIAGNOSTIC_AREA_M2],
        "donem": "SEZON_ACIK" if season_open else "YASAK_KALIBRASYON",
        "operasyonel_kural": (
            "15 Eylül ve sonrasında güçlü temporal-lokal aday yüksek diagnostik ağırlık alır; "
            "bu katman yine alarm veya saha görevi üretmez."
            if season_open
            else "15 Eylül öncesinde yalnız kalibrasyon/izleme; alarm veya saha görevi yok."
        ),
        "filtreler": {
            "ani_baslangic_zorunlu": True,
            "lokal_ani_baslangic_zorunlu": True,
            "yaygin_cevre_riski_reddet": True,
            "minimum_yerellik_orani": MIN_LOCALITY_RATIO,
            "minimum_ic_ve_cevre_gecerli_oran": round(MIN_VALID_RATIO, 4),
            "minimum_ic_bsi_degisim": MIN_INNER_BSI_CHANGE,
            "aktif_gorev_min_mesafe_m": MIN_ACTIVE_DISTANCE_M,
            "adaylar_arasi_min_mesafe_m": MIN_SELECTED_DISTANCE_M,
        },
        "aday_sayisi": len(selected),
        "adaylar": selected,
        "not": (
            "Kesin adres/ada/parsel/hukuki statü türetilmez. 'Mevki doğrulanmadı' etiketi "
            "olduğu gibi korunur. Bu dosya ana 250 m² üretim eşiğini değiştirmez."
        ),
    }


def _markdown(selected, report_payload):
    day = _report_day(report_payload)
    season_open = bool(day and day >= SEASON_START)
    lines = [
        SECTION_TITLE,
        "",
        "> **Alarm veya saha görevi değildir.** 250–900 m² temporal kuru-zemin havuzunda "
        "yalnız ani başlangıç + güçlü lokal/kompakt değişim birlikte görülen, çevresi yaygın "
        "hareket göstermeyen ve veri kalitesi yeterli noktaları görünür tutar. "
        + (
            "15 Eylül sonrası bunlar yüksek diagnostik ağırlıkla izlenir; görev açma kuralı değişmez."
            if season_open
            else "15 Eylül öncesinde yalnız kalibrasyon/izleme amaçlıdır; ekip rotasına otomatik eklenmez."
        ),
        "",
    ]
    if not selected:
        lines.extend(["Bu turda güvenli temporal-lokal erken sinyal yok.", ""])
        return "\n".join(lines)

    for index, item in enumerate(selected, start=1):
        neighborhood = str(item.get("mahalle") or "Mevki doğrulanmadı")
        area = int(max(_number(item.get("alan_m2"), 0), 0))
        locality = _number(item.get("yerellik_orani"), 0)
        bsi = abs(_number(item.get("ic_3x3_son_bsi_degisim"), 0))
        lines.append(
            f"{index}. **TEMPORAL-LOKAL — {neighborhood}** · yaklaşık {area:,} m² · "
            f"yerellik {locality:.2f} · iç BSI Δ {bsi:.3f} · "
            f"[Yol tarifi]({item.get('harita')}) · "
            f"[Parsel Sorgu'da aç]({item.get('parsel_sorgu')})".replace(",", ".")
        )
        lines.append(
            "   - İzleme notu: Koordinat değişim kümesinin yaklaşık merkezidir; gerçek "
            "hafriyat/kazı/temel olup olmadığı saha teyidi olmadan kesinleştirilmez."
        )
    lines.append("")
    return "\n".join(lines)


def _next_heading_index(text, start):
    marker = text.find("\n## ", max(int(start), 0))
    return marker + 1 if marker >= 0 else len(text)


def _inject_section(text, section):
    source = str(text or "")
    rendered = str(section or "").rstrip() + "\n"
    start = source.find(SECTION_TITLE)
    if start >= 0:
        end = _next_heading_index(source, start + len(SECTION_TITLE))
        prefix = source[:start].rstrip()
        suffix = source[end:].lstrip("\n")
        return (prefix + "\n\n" if prefix else "") + rendered.rstrip() + "\n\n" + suffix

    for anchor in INSERT_BEFORE:
        marker = source.find(anchor)
        if marker >= 0:
            prefix = source[:marker].rstrip()
            suffix = source[marker:].lstrip("\n")
            return (prefix + "\n\n" if prefix else "") + rendered.rstrip() + "\n\n" + suffix
    return source.rstrip() + "\n\n" + rendered if source.strip() else rendered


def update_watch():
    if not TEMPORAL_LOCALITY.exists() or not REPORT_JSON.exists():
        return []

    audit = json.loads(TEMPORAL_LOCALITY.read_text(encoding="utf-8"))
    report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    selected = select_watch(audit, report)
    output = _payload(audit, report, selected)

    OUTPUT_JSON.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    report["temporal_lokal_erken_izleme"] = selected
    report["temporal_lokal_erken_izleme_notu"] = output["operasyonel_kural"]
    REPORT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if FIELD_REPORT_MD.exists():
        current = FIELD_REPORT_MD.read_text(encoding="utf-8")
        FIELD_REPORT_MD.write_text(
            _inject_section(current, _markdown(selected, report)),
            encoding="utf-8",
        )
    return selected


def _self_check():
    strong = {
        "mahalle": "Mevki doğrulanmadı",
        "enlem": 38.40,
        "boylam": 26.62,
        "alan_m2": 300,
        "ani_baslangic_destegi": True,
        "lokal_ani_baslangic_destegi": True,
        "yaygin_cevre_degisim_riski": False,
        "ic_3x3_gecerli_oran": 0.78,
        "cevre_halka_gecerli_oran": 0.88,
        "yerellik_orani": 3.3,
        "ic_3x3_son_bsi_degisim": 0.115,
    }
    wide = dict(strong, enlem=38.41, yaygin_cevre_degisim_riski=True)
    weak = dict(strong, enlem=38.42, yerellik_orani=1.2)
    bad_quality = dict(strong, enlem=38.43, cevre_halka_gecerli_oran=0.5)
    audit = {
        "rapor_tarihi": "2026-09-05",
        "olusturma": "2026-09-05 19:22 +0300",
        "bolgeler": {
            "uzunkuyu": {
                "durum": "ok",
                "bolge": "Uzunkuyu · Germiyan · Ildır · Gülbahçe",
                "onceki_item": "OLD",
                "son_item": "NEW",
                "adaylar": [strong, wide, weak, bad_quality],
            }
        },
    }
    report = {"rapor_tarihi": "2026-09-05", "saha_adaylari": []}
    chosen = select_watch(audit, report, local_day=date(2026, 9, 5))
    assert len(chosen) == 1, chosen
    assert chosen[0]["alarm"] is False and chosen[0]["saha_gorevi"] is False
    assert chosen[0]["operasyonel_agirlik"] == "KALIBRASYON_DIAGNOSTIK"

    blocked_report = {
        "rapor_tarihi": "2026-09-05",
        "saha_adaylari": [
            {
                "saha_durumu": "KONTROLE_GIT",
                "enlem": 38.4003,
                "boylam": 26.6203,
            }
        ],
    }
    assert not select_watch(audit, blocked_report, local_day=date(2026, 9, 5))

    stale = dict(audit, rapor_tarihi="2026-09-04")
    assert not select_watch(stale, report, local_day=date(2026, 9, 5))

    post = select_watch(audit, report, local_day=date(2026, 9, 15))
    assert post and post[0]["operasyonel_agirlik"] == "YUKSEK_DIAGNOSTIK"

    sample = (
        "# Rapor\n\n## Ek kuru zemin kalibrasyon kontrolü\n\nX\n\n"
        "## Kör alan saha devriyesi\n\nKOR\n\n## Bugün sahada kontrol edilecek uydu adayları\n"
    )
    once = _inject_section(sample, _markdown(chosen, report))
    twice = _inject_section(once, _markdown(chosen, report))
    assert once == twice
    assert once.count(SECTION_TITLE) == 1
    assert "KOR" in once
    assert once.find(SECTION_TITLE) < once.find("## Kör alan saha devriyesi")
    print("temporal_local_watch_guard self-check: OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        return
    selected = update_watch()
    print(
        "Temporal-lokal erken sinyal izleme güncellendi: "
        + (", ".join(f"{item['enlem']:.6f},{item['boylam']:.6f}" for item in selected)
           if selected else "güçlü aday yok")
    )


if __name__ == "__main__":
    main()
