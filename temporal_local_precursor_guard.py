"""Güçlü temporal-lokal kalibrasyon izini taze ana aday için öncül kanıt olarak saklar.

Alarm/görev üretmez; 250 m² ana eşiğini ve 150-249 m² MİKRO politikasını değiştirmez.
250-900 m² bandında ani + lokal, veri kalitesi yeterli ve yaygın çevre hareketi taşımayan
izleri sahneler arasında korur. 15 Eylül sonrasında yalnız zaten bağımsız taze 250 m²+
ana aday daha eski bu izle 25 m içinde örtüşürse kendi taze-kazı bandında sıralama kanıtı
kazanır. Kesin adres, ada/parsel veya hukuki statü türetilmez.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import postseason_excavation_priority_guard as base

ROOT = Path(__file__).resolve().parent
WATCH = ROOT / "temporal_local_watch.json"
HISTORY = ROOT / "temporal_local_precursor_watchlist.json"
REPORT = ROOT / "latest_report.json"
FIELD_MD = ROOT / "SAHA_RAPORU.md"
ISTANBUL = ZoneInfo("Europe/Istanbul")

SEASON_START = date(2026, 9, 15)
MAIN_MIN = 250
DIAG_MAX = 900
MIN_LOCALITY = 1.5
MIN_VALID = 2 / 3
MIN_BSI = 0.10
MERGE_M = 20.0
MATCH_M = 25.0
MAX_ROWS = 50
SCENE_RE = re.compile(r"_(\d{8})T")


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _day(value=None):
    if value is None:
        return datetime.now(ISTANBUL).date()
    if isinstance(value, datetime):
        return value.date() if value.tzinfo is None else value.astimezone(ISTANBUL).date()
    if isinstance(value, date):
        return value
    raise TypeError("date/datetime bekleniyor")


def _load(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _point(row):
    try:
        return float(row.get("enlem")), float(row.get("boylam"))
    except (AttributeError, TypeError, ValueError):
        return None


def _distance(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    mean_lat = math.radians((lat1 + lat2) / 2)
    return math.hypot((lat1 - lat2) * 110570, (lon1 - lon2) * 111320 * math.cos(mean_lat))


def _scene_day(item_id):
    match = SCENE_RE.search(str(item_id or ""))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def _empty_history():
    return {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": MAIN_MIN,
        "diagnostik_bant_m2": [MAIN_MIN, DIAG_MAX],
        "esleme_mesafesi_m": MATCH_M,
        "amac": (
            "15 Eylül öncesi güçlü 250-900 m² temporal-lokal ani zemin izini saklamak; "
            "sonraki bağımsız taze 250 m²+ ana adaya yalnız sıralama kanıtı sağlamak."
        ),
        "adaylar": [],
    }


def _valid_payload(payload):
    return bool(
        isinstance(payload, dict)
        and payload.get("alarm") is not True
        and payload.get("saha_gorevi") is not True
        and int(_num(payload.get("ana_uretim_esigi_m2"), 0)) == MAIN_MIN
        and tuple(payload.get("diagnostik_bant_m2") or ()) == (MAIN_MIN, DIAG_MAX)
    )


def _history(payload):
    if not _valid_payload(payload):
        return _empty_history()
    normalized = _empty_history()
    normalized["adaylar"] = [row for row in payload.get("adaylar") or [] if isinstance(row, dict)]
    return normalized


def _strong(row):
    area = _num((row or {}).get("alan_m2"), 0)
    return bool(
        isinstance(row, dict)
        and row.get("alarm") is not True
        and row.get("saha_gorevi") is not True
        and MAIN_MIN <= area <= DIAG_MAX
        and row.get("ani_baslangic_destegi") is True
        and row.get("lokal_ani_baslangic_destegi") is True
        and row.get("yaygin_cevre_degisim_riski") is not True
        and _num(row.get("ic_3x3_gecerli_oran"), 0) >= MIN_VALID
        and _num(row.get("cevre_halka_gecerli_oran"), 0) >= MIN_VALID
        and _num(row.get("yerellik_orani"), 0) >= MIN_LOCALITY
        and abs(_num(row.get("ic_3x3_son_bsi_degisim"), 0)) >= MIN_BSI
        and _point(row) is not None
        and _scene_day(row.get("son_sentinel_item")) is not None
    )


def _nearest_index(rows, candidate, max_m):
    point = _point(candidate)
    best = None
    for idx, row in enumerate(rows):
        other = _point(row)
        if point is None or other is None:
            continue
        distance = _distance(point, other)
        if distance <= max_m and (best is None or distance < best[0]):
            best = (distance, idx)
    return None if best is None else best[1]


def update_history(history_payload, watch_payload):
    payload = _history(history_payload)
    if not _valid_payload(watch_payload):
        return payload

    rows = list(payload["adaylar"])
    for candidate in watch_payload.get("adaylar") or []:
        if not _strong(candidate):
            continue
        scene_date = _scene_day(candidate.get("son_sentinel_item"))
        scene_id = str(candidate.get("son_sentinel_item") or "")
        lat, lon = _point(candidate)
        idx = _nearest_index(rows, candidate, MERGE_M)
        if idx is None:
            rows.append(
                {
                    "temporal_iz_id": f"TL-{scene_date:%Y%m%d}-{lat:.5f}-{lon:.5f}",
                    "alarm": False,
                    "saha_gorevi": False,
                    "enlem": round(lat, 6),
                    "boylam": round(lon, 6),
                    "alan_m2": round(_num(candidate.get("alan_m2"), 0)),
                    "bolge_anahtari": str(candidate.get("bolge_anahtari") or ""),
                    "bolge": str(candidate.get("bolge") or ""),
                    "mahalle": str(candidate.get("mahalle") or "Mevki doğrulanmadı"),
                    "ilk_guclu_tarih": scene_date.strftime("%d.%m.%Y"),
                    "son_guclu_tarih": scene_date.strftime("%d.%m.%Y"),
                    "son_guclu_sentinel_item": scene_id,
                    "sentinel_sahneleri": [scene_id],
                    "maks_yerellik_orani": round(_num(candidate.get("yerellik_orani"), 0), 3),
                    "maks_ic_bsi_degisim": round(abs(_num(candidate.get("ic_3x3_son_bsi_degisim"), 0)), 4),
                }
            )
            continue

        old = dict(rows[idx])
        old_date = base._parse_scene_date(old.get("son_guclu_tarih"))
        if old_date is not None and scene_date < old_date:
            continue
        scenes = [str(value) for value in old.get("sentinel_sahneleri") or [] if str(value)]
        if scene_id not in scenes:
            scenes.append(scene_id)
        old.update(
            {
                "alarm": False,
                "saha_gorevi": False,
                "enlem": round(lat, 6),
                "boylam": round(lon, 6),
                "alan_m2": round(_num(candidate.get("alan_m2"), 0)),
                "bolge_anahtari": str(candidate.get("bolge_anahtari") or old.get("bolge_anahtari") or ""),
                "bolge": str(candidate.get("bolge") or old.get("bolge") or ""),
                "mahalle": str(candidate.get("mahalle") or old.get("mahalle") or "Mevki doğrulanmadı"),
                "son_guclu_tarih": scene_date.strftime("%d.%m.%Y"),
                "son_guclu_sentinel_item": scene_id,
                "sentinel_sahneleri": scenes[-6:],
                "maks_yerellik_orani": round(max(_num(old.get("maks_yerellik_orani"), 0), _num(candidate.get("yerellik_orani"), 0)), 3),
                "maks_ic_bsi_degisim": round(max(_num(old.get("maks_ic_bsi_degisim"), 0), abs(_num(candidate.get("ic_3x3_son_bsi_degisim"), 0))), 4),
            }
        )
        rows[idx] = old

    rows.sort(
        key=lambda row: (
            base._parse_scene_date(row.get("son_guclu_tarih")) or date.min,
            _num(row.get("maks_yerellik_orani"), 0),
        ),
        reverse=True,
    )
    payload["adaylar"] = rows[:MAX_ROWS]
    payload["guclu_iz_sayisi"] = len(payload["adaylar"])
    payload["guncelleme_tarihi"] = str(watch_payload.get("kaynak_olusturma") or "")
    return payload


def _match(item, history_payload):
    if not base._is_fresh_excavation_candidate(item):
        return None
    current = base._parse_scene_date(item.get("son_tarih"))
    point = _point(item)
    if current is None or point is None:
        return None

    best = None
    for row in (history_payload or {}).get("adaylar") or []:
        previous = base._parse_scene_date((row or {}).get("son_guclu_tarih"))
        other = _point(row)
        if previous is None or previous >= current or other is None:
            continue
        distance = _distance(point, other)
        if distance > MATCH_M:
            continue
        score = (_num(row.get("maks_yerellik_orani"), 0), -distance)
        if best is None or score > best[0]:
            best = (
                score,
                {
                    "temporal_lokal_oncul_eslesmesi": True,
                    "temporal_lokal_oncul_iz_id": str(row.get("temporal_iz_id") or ""),
                    "temporal_lokal_oncul_mesafe_m": round(distance, 1),
                    "temporal_lokal_oncul_son_guclu_tarih": previous.strftime("%d.%m.%Y"),
                    "temporal_lokal_oncul_son_guclu_sentinel_item": str(row.get("son_guclu_sentinel_item") or ""),
                    "temporal_lokal_oncul_yerellik_orani": round(_num(row.get("maks_yerellik_orani"), 0), 2),
                    "temporal_lokal_oncul_kanit_notu": (
                        "Daha eski sahnede güçlü 250-900 m² ani+lokal zemin değişimi aynı noktada görüldü. "
                        "Bu yalnız zaten bağımsız taze 250 m²+ ana adayın sıralama kanıtıdır."
                    ),
                },
            )
    return None if best is None else best[1]


def _annotate(item, history_payload):
    for key in list(item):
        if key.startswith("temporal_lokal_oncul_"):
            item.pop(key, None)
    matched = _match(item, history_payload)
    if matched:
        item.update(matched)
    return item


def select_shortlist(candidates, history_payload, limit=base.route.SHORTLIST_LIMIT):
    cap = max(int(limit), 0)
    if cap <= 0:
        return []
    micro = base._load_micro_watchlist()
    indexed = list(enumerate(base.freshness._normalized_actionable_candidates(candidates)))

    def key(pair):
        idx, item = pair
        prior = base._sort_key(item, idx, micro)
        temporal_rank = 0 if prior[0] == 1 and _match(item, history_payload) else 1
        return (prior[0], prior[1], temporal_rank, *prior[2:])

    indexed.sort(key=key)
    ranked = [item for _, item in indexed]
    selected = [dict(item) for item in ranked[:cap]]
    selected = base._balance_regions(ranked, selected, cap)
    for order, item in enumerate(selected, start=1):
        base._annotate_micro_precursor(item, micro)
        _annotate(item, history_payload)
        item["gunluk_sira"] = order
    return selected


def _markdown(shortlist):
    text = base._shortlist_markdown(shortlist)
    for item in shortlist:
        if item.get("temporal_lokal_oncul_eslesmesi") is not True:
            continue
        task_id = str(item.get("gorev_id") or "-")
        needle = f" · Görev `{task_id}`"
        text = text.replace(needle, f" · **TEMPORAL→ANA DEVAM KANITI**{needle}", 1)
    return text


def apply(local_day=None):
    before = _history(_load(HISTORY))
    after = update_history(before, _load(WATCH))
    old_text = HISTORY.read_text(encoding="utf-8") if HISTORY.exists() else ""
    new_text = json.dumps(after, ensure_ascii=False, indent=2) + "\n"
    history_changed = old_text != new_text
    if history_changed:
        HISTORY.write_text(new_text, encoding="utf-8")

    if _day(local_day) < SEASON_START:
        return history_changed, []

    report = _load(REPORT)
    if not isinstance(report, dict):
        return history_changed, []
    shortlist = select_shortlist(report.get("saha_adaylari") or [], after)
    report["gunun_ilk_3_kontrolu"] = shortlist
    meta = report.get("postseason_excavation_priority")
    meta = meta if isinstance(meta, dict) else {}
    meta.update(
        {
            "temporal_lokal_oncul_esleme_mesafesi_m": MATCH_M,
            "temporal_lokal_oncul_eslesen_gorevler": [
                str(item.get("gorev_id") or "")
                for item in shortlist
                if item.get("temporal_lokal_oncul_eslesmesi") is True
            ],
            "temporal_lokal_oncul_notu": (
                "Daha eski güçlü 250-900 m² ani+lokal diagnostik yalnız zaten bağımsız taze "
                "250 m²+ ana aday için sıralama kanıtıdır; alarm/görev üretmez."
            ),
        }
    )
    report["postseason_excavation_priority"] = meta

    report_before = REPORT.read_text(encoding="utf-8")
    report_after = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    report_changed = report_before != report_after
    if report_changed:
        REPORT.write_text(report_after, encoding="utf-8")

    md_changed = False
    if FIELD_MD.exists():
        current = FIELD_MD.read_text(encoding="utf-8")
        updated = base.route._inject_markdown(current, _markdown(shortlist))
        if updated != current:
            FIELD_MD.write_text(updated, encoding="utf-8")
            md_changed = True
    return history_changed or report_changed or md_changed, shortlist


def _self_check():
    base._self_check()
    watch = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": 250,
        "diagnostik_bant_m2": [250, 900],
        "kaynak_olusturma": "2026-09-07 15:27 +0300",
        "adaylar": [
            {
                "mahalle": "Gülbahçe",
                "enlem": 38.3328,
                "boylam": 26.6456,
                "alan_m2": 300,
                "ani_baslangic_destegi": True,
                "lokal_ani_baslangic_destegi": True,
                "yaygin_cevre_degisim_riski": False,
                "ic_3x3_gecerli_oran": 0.778,
                "cevre_halka_gecerli_oran": 0.875,
                "yerellik_orani": 3.22,
                "ic_3x3_son_bsi_degisim": 0.107,
                "bolge_anahtari": "uzunkuyu",
                "bolge": base.freshness.CANONICAL_EAST_REGION,
                "son_sentinel_item": "S2A_T35SMC_20260905T090529_L2A",
                "alarm": False,
                "saha_gorevi": False,
            }
        ],
    }
    history = update_history({}, watch)
    assert history["guclu_iz_sayisi"] == 1

    matched = {
        "gorev_id": "TEMP_MATCH",
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "ERKEN",
        "mahalle": "Gülbahçe",
        "enlem": 38.3329,
        "boylam": 26.6456,
        "alan_m2": 600,
        "bolge": base.freshness.CANONICAL_EAST_REGION,
        "onceki_tarih": "05.09.2026",
        "son_tarih": "15.09.2026",
        "yeni_goruntu": True,
        "uydu_onceligi": "YÜKSEK",
        "boyut_sinifi": "KUCUK",
        "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
    }
    plain = dict(matched)
    plain.update({"gorev_id": "PLAIN", "enlem": 38.36, "boylam": 26.68})
    same_day = dict(matched)
    same_day["son_tarih"] = "05.09.2026"
    micro = dict(matched)
    micro["alan_m2"] = 200

    assert _match(matched, history)
    assert _match(plain, history) is None
    assert _match(same_day, history) is None
    assert _match(micro, history) is None
    selected = select_shortlist([plain, matched], history, limit=2)
    assert selected[0]["gorev_id"] == "TEMP_MATCH", selected
    assert selected[0]["temporal_lokal_oncul_eslesmesi"] is True
    assert MAIN_MIN == 250 and base.MICRO_MIN_M2 == 150 and base.MICRO_MAX_M2 == 249
    print("OK: temporal-lokal öncül hafıza self-check geçti.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        _self_check()
        return
    changed, shortlist = apply()
    if _day() < SEASON_START:
        print("Kalibrasyon modu: temporal-lokal öncül hafıza güncellendi; operasyon sırası değişmedi.")
        return
    matched = sum(item.get("temporal_lokal_oncul_eslesmesi") is True for item in shortlist)
    print(f"Temporal-lokal öncül kanıt uygulandı: ilk üçte {matched} eşleşme.")
    if not changed:
        print("Rapor ve öncül hafıza zaten güncel.")


if __name__ == "__main__":
    main()
