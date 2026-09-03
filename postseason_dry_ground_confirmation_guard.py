"""15 Eylül sonrası güçlü kuru-zemin anomalilerini tek günlük teyit rotasına bağlar.

Ana Sentinel üretim eşiği 250 m² olarak kalır. Bu katman üretim maskesi dışında kalan
250-900 m² kuru-zemin diagnostiklerinden yalnız aynı yeni Sentinel sahnesinde şu kanıtları
birlikte taşıyanları değerlendirir: izole/saha-benzeri geometri, lineer risk olmaması,
uzun zaman serisinde ani başlangıç, kararlı geçmiş zemin, yörünge/geometri riski olmaması
ve 5x5 çevre halkasına göre lokal değişim. Geniş/homojen çevre hareketi elenir.

Katman yeni alarm veya kalıcı saha görevi üretmez. En fazla bir güçlü diagnostik, yalnız
15 Eylül 2026 ve sonrasında ve ilgili bölgenin o gün gerçekten yeni Sentinel görüntüsü
aldığı doğrulanırsa, mevcut ilk-3 rotada eski/backlog bir görevin önüne tek günlük
"DOĞRULAMA" olarak girebilir. TEKRAR_GIT ve ana üretimden gelen taze hafriyat adayları
asla düşürülmez. Aynı noktada zaten aktif ana görev varsa diagnostik tekrar eklenmez.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import daily_route_freshness_guard as freshness
import daily_route_shortlist as route
import postseason_excavation_priority_guard as postseason
from scanner import DB


ISTANBUL = ZoneInfo("Europe/Istanbul")
FULL_OPERATION_START = date(2026, 9, 15)
MAIN_ALARM_MIN_M2 = 250
DRY_GROUND_MAX_M2 = 900
MAX_CONFIRMATIONS = 1
DUPLICATE_RADIUS_M = 40.0
MIN_LOCALITY_RATIO = 1.5
REPORT_JSON = Path(__file__).with_name("latest_report.json")
FIELD_REPORT_MD = Path(__file__).with_name("SAHA_RAPORU.md")
TEMPORAL_AUDIT = Path(__file__).with_name("dry_ground_temporal_audit.json")
LOCALITY_AUDIT = Path(__file__).with_name("temporal_locality_audit.json")
NOTE_SUFFIX = (
    " Üretim maskesi dışında kalan 250-900 m² kuru-zemin değişimi ancak yeni Sentinel "
    "sahnesinde izole + uzun-temporal ani başlangıç + lokal çevre kontrastı birlikte "
    "doğrulanırsa tek günlük DOĞRULAMA olarak eski backlog'un önüne girebilir; bu kayıt "
    "alarm veya kalıcı görev değildir."
)


def _load_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


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
    raise TypeError("local_day date/datetime olmalı")


def _parse_scene_date(value):
    text = str(value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _point_key(item):
    try:
        return (round(float(item.get("enlem")), 5), round(float(item.get("boylam")), 5))
    except (TypeError, ValueError):
        return None


def _distance_m(a, b):
    try:
        lat1, lon1 = float(a.get("enlem")), float(a.get("boylam"))
        lat2, lon2 = float(b.get("enlem")), float(b.get("boylam"))
    except (TypeError, ValueError):
        return float("inf")
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def _canonical_region(label):
    text = str(label or "").strip()
    if text == freshness.LEGACY_EAST_REGION:
        return freshness.CANONICAL_EAST_REGION
    return text


def _new_scene_regions(report_date, db_path=DB):
    """O gün DB'de gerçekten yeni görüntü diye kaydedilmiş bölge -> son_item haritası."""
    path = Path(db_path)
    if not path.exists():
        return {}
    try:
        with sqlite3.connect(path) as connection:
            rows = connection.execute(
                """SELECT bolge,son_item,yeni_goruntu
                FROM gunluk_uydu_raporlari WHERE rapor_tarihi=?""",
                (str(report_date or ""),),
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {
        str(region): str(last_item)
        for region, last_item, is_new in rows
        if int(is_new or 0) == 1 and last_item
    }


def _temporal_map(region_data):
    mapped = {}
    for raw in (region_data or {}).get("adaylar") or []:
        if not isinstance(raw, dict):
            continue
        key = _point_key(raw)
        if key is not None:
            mapped[key] = raw
    return mapped


def _is_strong_pair(temporal_item, locality_item):
    area = _number(temporal_item.get("alan_m2"), 0)
    local_area = _number(locality_item.get("alan_m2"), 0)
    if not (MAIN_ALARM_MIN_M2 <= area <= DRY_GROUND_MAX_M2):
        return False
    if abs(area - local_area) > 1:
        return False
    return bool(
        temporal_item.get("ani_baslangic_destegi") is True
        and temporal_item.get("istikrarsiz_zemin_riski") is False
        and temporal_item.get("uzun_temporal_istikrarsiz_zemin_riski") is False
        and str(temporal_item.get("uzun_temporal_koruma") or "").upper() == "KORUNDU"
        and temporal_item.get("yorunge_geometri_riski") is False
        and temporal_item.get("izole_saha_benzeri") is True
        and temporal_item.get("lineer_geometri_riski") is False
        and locality_item.get("lokal_ani_baslangic_destegi") is True
        and locality_item.get("yaygin_cevre_degisim_riski") is False
        and _number(locality_item.get("yerellik_orani"), 0) >= MIN_LOCALITY_RATIO
    )


def _near_existing_task(candidate, report_payload):
    for item in (report_payload or {}).get("saha_adaylari") or []:
        if isinstance(item, dict) and _distance_m(candidate, item) <= DUPLICATE_RADIUS_M:
            return True
    return False


def select_confirmations(report_payload, temporal_payload, locality_payload, new_regions, local_day=None):
    day = _local_day(local_day)
    if day < FULL_OPERATION_START:
        return []
    if not all(isinstance(value, dict) for value in (report_payload, temporal_payload, locality_payload)):
        return []

    report_date = str(report_payload.get("rapor_tarihi") or "")
    if not report_date or str(temporal_payload.get("rapor_tarihi") or "") != report_date:
        return []
    if str(locality_payload.get("rapor_tarihi") or "") != report_date:
        return []

    temporal_regions = temporal_payload.get("bolgeler") or {}
    locality_regions = locality_payload.get("bolgeler") or {}
    if not isinstance(temporal_regions, dict) or not isinstance(locality_regions, dict):
        return []

    selected = []
    for region_key, local_region in locality_regions.items():
        temporal_region = temporal_regions.get(region_key)
        if not isinstance(local_region, dict) or not isinstance(temporal_region, dict):
            continue
        if local_region.get("durum") != "ok" or temporal_region.get("durum") != "ok":
            continue

        temporal_last = str(temporal_region.get("son_item") or "")
        locality_last = str(local_region.get("son_item") or "")
        if not temporal_last or temporal_last != locality_last:
            continue
        if str((new_regions or {}).get(region_key) or "") != temporal_last:
            continue

        scene_date = _parse_scene_date(temporal_region.get("son_tarih"))
        if scene_date is None or scene_date < FULL_OPERATION_START:
            continue

        temporal_by_point = _temporal_map(temporal_region)
        for local_item in local_region.get("adaylar") or []:
            if not isinstance(local_item, dict):
                continue
            temporal_item = temporal_by_point.get(_point_key(local_item))
            if not isinstance(temporal_item, dict) or not _is_strong_pair(temporal_item, local_item):
                continue

            candidate = {
                "oncelik": "DOĞRULAMA",
                "mahalle": str(temporal_item.get("mahalle") or "Mevki doğrulanmadı"),
                "enlem": round(_number(temporal_item.get("enlem")), 6),
                "boylam": round(_number(temporal_item.get("boylam")), 6),
                "alan_m2": round(_number(temporal_item.get("alan_m2"))),
                "sinyal": (
                    "Üretim maskesi dışında; yeni Sentinel sahnesinde izole, uzun-temporal "
                    "ani ve lokal kuru-zemin değişimi. Hafriyat/temel saha doğrulaması."
                ),
                "bolge": _canonical_region(temporal_region.get("bolge")),
                "onceki_tarih": temporal_region.get("onceki_tarih"),
                "son_tarih": temporal_region.get("son_tarih"),
                "yeni_goruntu": True,
                "saha_durumu": "DIAGNOSTIK_DOGRULAMA",
                "alarm": False,
                "saha_gorevi": False,
                "boyut_sinifi": "KURU_ZEMIN_DOGRULAMA",
                "uydu_onceligi": "YÜKSEK",
                "postseason_kuru_zemin_dogrulama": True,
                "yerellik_orani": _number(local_item.get("yerellik_orani"), 0),
                "uzun_temporal_ani_baslangic_orani": _number(
                    temporal_item.get("uzun_temporal_ani_baslangic_orani"), 0
                ),
                "son_cift_bsi_degisim": _number(temporal_item.get("son_cift_bsi_degisim"), 0),
                "konum_notu": (
                    "Sentinel kuru-zemin diagnostik kümesinin yaklaşık merkezidir; kesin adres, "
                    "ada veya parsel değildir. Üretim alarmı değildir ve sahada doğrulanmalıdır."
                ),
            }
            candidate["harita"] = (
                "https://www.google.com/maps/dir/?api=1&destination="
                f"{candidate['enlem']:.6f},{candidate['boylam']:.6f}"
            )
            digest = hashlib.sha1(
                f"{region_key}|{temporal_last}|{candidate['enlem']:.5f}|{candidate['boylam']:.5f}".encode("utf-8")
            ).hexdigest()[:10].upper()
            candidate["gorev_id"] = f"DG{digest}"
            if not _near_existing_task(candidate, report_payload):
                selected.append(candidate)

    selected.sort(
        key=lambda item: (
            -_number(item.get("yerellik_orani"), 0),
            -_number(item.get("uzun_temporal_ani_baslangic_orani"), 0),
            -_number(item.get("son_cift_bsi_degisim"), 0),
            _number(item.get("alan_m2"), 0),
        )
    )
    return selected


def _existing_band(item):
    if postseason._is_manual_repeat(item):
        return 0
    if postseason._is_fresh_excavation_candidate(item):
        return 1
    return 3


def merge_confirmation(shortlist, confirmations, limit=route.SHORTLIST_LIMIT):
    current = [dict(item) for item in shortlist if isinstance(item, dict)]
    cap = max(int(limit), 0)
    if cap <= 0 or not confirmations:
        return current[:cap]

    candidate = dict(confirmations[0])
    if any(_distance_m(candidate, item) <= DUPLICATE_RADIUS_M for item in current):
        return current[:cap]

    insert_at = next((i for i, item in enumerate(current) if _existing_band(item) >= 3), len(current))
    if len(current) < cap:
        current.insert(insert_at, candidate)
    else:
        candidate_region = _canonical_region(candidate.get("bolge"))
        same_region_backlog = [
            i for i, item in enumerate(current)
            if _existing_band(item) >= 3 and _canonical_region(item.get("bolge")) == candidate_region
        ]
        replace_index = same_region_backlog[-1] if same_region_backlog else None
        if replace_index is None:
            candidate_region_present = any(
                _canonical_region(item.get("bolge")) == candidate_region for item in current
            )
            if not candidate_region_present:
                other_backlog = [i for i, item in enumerate(current) if _existing_band(item) >= 3]
                replace_index = other_backlog[-1] if other_backlog else None
        if replace_index is not None:
            current[replace_index] = candidate

    current.sort(
        key=lambda item: (
            0 if postseason._is_manual_repeat(item) else
            1 if postseason._is_fresh_excavation_candidate(item) else
            2 if item.get("postseason_kuru_zemin_dogrulama") else 3,
            int(item.get("gunluk_sira") or 99),
        )
    )
    current = current[:cap]
    for index, item in enumerate(current, start=1):
        item["gunluk_sira"] = index
    return current


def _shortlist_markdown(shortlist, note):
    lines = [route.SECTION_TITLE, "", f"> {note}", ""]
    if not shortlist:
        return "\n".join(lines + ["Bugün için eyleme dönük aktif uydu görevi yok.", ""])
    for item in shortlist:
        order = int(item.get("gunluk_sira") or 0)
        priority = str(item.get("oncelik") or "KONTROL")
        neighborhood = str(item.get("mahalle") or "Konum araştırılıyor")
        area = _number(item.get("alan_m2"), 0)
        area_text = f" · yaklaşık {int(area):,} m²".replace(",", ".") if area else ""
        tag = " · **KURU ZEMİN TEYİDİ**" if item.get("postseason_kuru_zemin_dogrulama") else ""
        map_url = str(item.get("harita") or "").strip()
        route_text = f" · [Yol tarifi]({map_url})" if map_url.startswith(("http://", "https://")) else ""
        lines.append(f"{order}. **{priority} — {neighborhood}**{area_text}{tag}{route_text}")
    lines.append("")
    return "\n".join(lines)


def apply_confirmation(local_day=None, db_path=DB):
    day = _local_day(local_day)
    if day < FULL_OPERATION_START:
        return False, []
    report = _load_json(REPORT_JSON)
    temporal_payload = _load_json(TEMPORAL_AUDIT)
    locality_payload = _load_json(LOCALITY_AUDIT)
    if not all(isinstance(value, dict) for value in (report, temporal_payload, locality_payload)):
        return False, []

    new_regions = _new_scene_regions(report.get("rapor_tarihi"), db_path=db_path)
    confirmations = select_confirmations(
        report, temporal_payload, locality_payload, new_regions, local_day=day
    )
    chosen = confirmations[:MAX_CONFIRMATIONS]
    current = report.get("gunun_ilk_3_kontrolu") or []
    merged = merge_confirmation(current, chosen)

    note = str(report.get("gunun_ilk_3_notu") or postseason.NOTE).strip()
    if NOTE_SUFFIX.strip() not in note:
        note = (note + NOTE_SUFFIX).strip()
    report["gunun_ilk_3_kontrolu"] = merged
    report["gunun_ilk_3_notu"] = note
    report["postseason_kuru_zemin_dogrulama"] = {
        "aktif": True,
        "baslangic_tarihi": FULL_OPERATION_START.isoformat(),
        "ana_alarm_alt_esigi_m2": MAIN_ALARM_MIN_M2,
        "diagnostik_aralik_m2": [MAIN_ALARM_MIN_M2, DRY_GROUND_MAX_M2],
        "yeni_sahne_bolgeleri": sorted(new_regions),
        "guclu_aday_sayisi": len(confirmations),
        "rotaya_alinan": [item.get("gorev_id") for item in merged if item.get("postseason_kuru_zemin_dogrulama")],
        "alarm": False,
        "kalici_saha_gorevi": False,
        "kural": (
            "Yalnız o gün yeni Sentinel sahnesi + uzun-temporal ani başlangıç + kararlı geçmiş "
            "zemin + yörünge güveni + izole/non-lineer geometri + lokal 5x5 çevre kontrastı. "
            "TEKRAR_GIT ve taze ana üretim hafriyat adayları düşürülmez."
        ),
    }

    before = REPORT_JSON.read_text(encoding="utf-8")
    after = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    changed = before != after
    if changed:
        REPORT_JSON.write_text(after, encoding="utf-8")

    if FIELD_REPORT_MD.exists():
        current_md = FIELD_REPORT_MD.read_text(encoding="utf-8")
        updated_md = route._inject_markdown(current_md, _shortlist_markdown(merged, note))
        if updated_md != current_md:
            FIELD_REPORT_MD.write_text(updated_md, encoding="utf-8")
            changed = True
    return changed, chosen


def _self_check():
    west = freshness.CANONICAL_WEST_REGION
    temporal_item = {
        "mahalle": "Ovacık", "enlem": 38.25, "boylam": 26.32, "alan_m2": 500,
        "ani_baslangic_destegi": True, "istikrarsiz_zemin_riski": False,
        "uzun_temporal_istikrarsiz_zemin_riski": False, "uzun_temporal_koruma": "KORUNDU",
        "yorunge_geometri_riski": False, "izole_saha_benzeri": True,
        "lineer_geometri_riski": False, "uzun_temporal_ani_baslangic_orani": 8.0,
        "son_cift_bsi_degisim": 0.20,
    }
    locality_item = {
        "mahalle": "Ovacık", "enlem": 38.25, "boylam": 26.32, "alan_m2": 500,
        "lokal_ani_baslangic_destegi": True, "yaygin_cevre_degisim_riski": False,
        "yerellik_orani": 2.2,
    }
    report = {"rapor_tarihi": "2026-09-16", "saha_adaylari": []}
    temporal_payload = {
        "rapor_tarihi": "2026-09-16", "bolgeler": {"cesme": {
            "durum": "ok", "bolge": west, "onceki_tarih": "13.09.2026",
            "son_tarih": "16.09.2026", "son_item": "S2_NEW", "adaylar": [temporal_item],
        }}
    }
    locality_payload = {
        "rapor_tarihi": "2026-09-16", "bolgeler": {"cesme": {
            "durum": "ok", "bolge": west, "son_item": "S2_NEW", "adaylar": [locality_item],
        }}
    }
    selected = select_confirmations(
        report, temporal_payload, locality_payload, {"cesme": "S2_NEW"},
        local_day=date(2026, 9, 16),
    )
    assert len(selected) == 1 and selected[0]["alarm"] is False
    assert not select_confirmations(
        report, temporal_payload, locality_payload, {"cesme": "S2_NEW"},
        local_day=date(2026, 9, 14),
    )

    # Eski sahne, geniş çevre veya mevcut ana görev aynı noktadaysa yükseltme yok.
    old_temporal = json.loads(json.dumps(temporal_payload))
    old_temporal["bolgeler"]["cesme"]["son_tarih"] = "12.09.2026"
    assert not select_confirmations(
        report, old_temporal, locality_payload, {"cesme": "S2_NEW"},
        local_day=date(2026, 9, 16),
    )
    broad_locality = json.loads(json.dumps(locality_payload))
    broad_locality["bolgeler"]["cesme"]["adaylar"][0]["yaygin_cevre_degisim_riski"] = True
    broad_locality["bolgeler"]["cesme"]["adaylar"][0]["lokal_ani_baslangic_destegi"] = False
    assert not select_confirmations(
        report, temporal_payload, broad_locality, {"cesme": "S2_NEW"},
        local_day=date(2026, 9, 16),
    )
    duplicate_report = {**report, "saha_adaylari": [{"enlem": 38.2501, "boylam": 26.3201}]}
    assert not select_confirmations(
        duplicate_report, temporal_payload, locality_payload, {"cesme": "S2_NEW"},
        local_day=date(2026, 9, 16),
    )

    repeat = {"gorev_id": "R", "saha_durumu": "TEKRAR_GIT", "oncelik": "TEKRAR", "bolge": west}
    fresh = {
        "gorev_id": "F", "saha_durumu": "KONTROLE_GIT", "oncelik": "ERKEN",
        "alan_m2": 600, "bolge": west, "yeni_goruntu": True,
        "uydu_onceligi": "YÜKSEK", "boyut_sinifi": "KUCUK",
    }
    backlog = {"gorev_id": "B", "saha_durumu": "KONTROLE_GIT", "oncelik": "GECİKEN", "alan_m2": 900, "bolge": west}
    merged = merge_confirmation([repeat, fresh, backlog], selected)
    assert [item.get("gorev_id") for item in merged] == ["R", "F", selected[0]["gorev_id"]], merged
    assert all(_number(item.get("alan_m2"), MAIN_ALARM_MIN_M2) >= MAIN_ALARM_MIN_M2 for item in merged[1:])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.self_check:
        print("15 Eylül kuru-zemin teyit geçidi öz testi başarılı.")
    else:
        changed, chosen = apply_confirmation()
        print(
            "15 Eylül kuru-zemin teyit geçidi: "
            + (f"{len(chosen)} güçlü aday değerlendirildi; rapor güncellendi={changed}." if _local_day() >= FULL_OPERATION_START else "kalibrasyon dönemi, üretim raporuna dokunulmadı.")
        )
