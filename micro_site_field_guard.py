"""Mikro şantiye kısa listesini güncel saha geri bildirimiyle korur.

150-249 m² katman alarm/görev üretmez. Bu guard yalnız aynı Sentinel sahnesiyle
ilişkili ve yaklaşık aynı noktada daha önce saha sonucu bulunan mikro adayların
"yeni fırsat" gibi tekrar öne çıkmasını engeller. Eski saha sonucu yeni Sentinel
sahnesini kalıcı olarak bastırmaz; böylece bugün tarla olan yerde sonraki görüntüde
hafriyat başlarsa aday yeniden değerlendirilebilir.

Normal mikro kısa listeye ek olarak, spektral eşiği yalnız dar marjla kaçırıp
ayrı temporal + lokal analizde güçlü bulunan en fazla birkaç aday da yalnız
kalibrasyon incelemesine alınabilir. Bu ek yol alarm, saha görevi veya otomatik
15 Eylül terfisi üretmez.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime
from pathlib import Path


SHORTLIST_FILE = Path(__file__).with_name("micro_site_shortlist.json")
AUDIT_FILE = Path(__file__).with_name("micro_site_audit.json")
BORDERLINE_FILE = Path(__file__).with_name("micro_spectral_borderline_review.json")
DB_FILE = Path(__file__).with_name("santiye.db")
OUTPUT_FILE = Path(__file__).with_name("micro_site_field_review.json")
FIELD_MATCH_RADIUS_M = 35
BORDERLINE_DEDUPE_RADIUS_M = 35
MAX_BORDERLINE_FIELD_REVIEW = 2
KNOWN_OUTCOMES = {
    "SANTIYE_KAZI",
    "YOL_ALTYAPI",
    "TARLA_BITKI",
    "YANLIS_POZITIF",
}


def _number(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _distance_m(first, second):
    lat1 = _number(first.get("enlem"))
    lon1 = _number(first.get("boylam"))
    lat2 = _number(second.get("enlem"))
    lon2 = _number(second.get("boylam"))
    if None in (lat1, lon1, lat2, lon2):
        return float("inf")
    mean_lat = math.radians((lat1 + lat2) / 2)
    north_m = (lat2 - lat1) * 110570
    east_m = (lon2 - lon1) * 111320 * math.cos(mean_lat)
    return float(math.hypot(north_m, east_m))


def _parse_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    candidates = [text[:10], text]
    for candidate in candidates:
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                pass
    return None


def _columns(connection, table):
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _read_field_rows(db_path=DB_FILE):
    if not Path(db_path).exists():
        return []
    with sqlite3.connect(db_path, timeout=30) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "saha_sonuclari" not in tables:
            return []

        s_cols = _columns(connection, "saha_sonuclari")
        d_exists = "saha_durumlari" in tables
        d_cols = _columns(connection, "saha_durumlari") if d_exists else set()

        def coalesce_column(name):
            parts = []
            if name in s_cols:
                parts.append(f"s.{name}")
            if d_exists and name in d_cols:
                parts.append(f"d.{name}")
            if not parts:
                return "NULL"
            if len(parts) == 1:
                return parts[0]
            return f"COALESCE({','.join(parts)})"

        join = (
            "LEFT JOIN saha_durumlari d ON d.gorev_id=s.gorev_id"
            if d_exists and "gorev_id" in d_cols
            else ""
        )
        son_tarih_expr = "s.son_tarih" if "son_tarih" in s_cols else "NULL"
        kayit_expr = "s.kayit_zamani" if "kayit_zamani" in s_cols else "NULL"
        query = f"""SELECT s.gorev_id,s.sonuc,
        {coalesce_column('enlem')} AS enlem,
        {coalesce_column('boylam')} AS boylam,
        {son_tarih_expr} AS son_tarih,
        {kayit_expr} AS kayit_zamani
        FROM saha_sonuclari s {join}"""
        rows = []
        for task_id, outcome, latitude, longitude, scene_date, recorded_at in connection.execute(query):
            outcome = str(outcome or "").strip().upper()
            if outcome not in KNOWN_OUTCOMES:
                continue
            latitude = _number(latitude)
            longitude = _number(longitude)
            if latitude is None or longitude is None:
                continue
            rows.append(
                {
                    "gorev_id": str(task_id or ""),
                    "sonuc": outcome,
                    "enlem": latitude,
                    "boylam": longitude,
                    "son_tarih": str(scene_date or ""),
                    "kayit_zamani": str(recorded_at or ""),
                }
            )
        return rows


def _scene_dates(audit_payload):
    dates = {}
    for region_key, region in (audit_payload.get("bolgeler") or {}).items():
        if isinstance(region, dict):
            dates[str(region_key)] = str(region.get("son_tarih") or "")
    return dates


def _feedback_effective_date(row):
    return _parse_date(row.get("son_tarih")) or _parse_date(row.get("kayit_zamani"))


def _applicable_feedback(candidate, scene_date, field_rows):
    candidate_scene = _parse_date(scene_date)
    matches = []
    for field in field_rows:
        distance = _distance_m(candidate, field)
        if distance > FIELD_MATCH_RADIUS_M:
            continue
        effective = _feedback_effective_date(field)
        # Eski bir saha kararı, daha yeni Sentinel sahnesini otomatik bastırmamalı.
        if candidate_scene is not None and effective is not None and effective < candidate_scene:
            continue
        match = dict(field)
        match["mesafe_m"] = round(distance, 1)
        match["etkin_tarih"] = effective.isoformat() if effective else None
        matches.append(match)
    matches.sort(key=lambda item: (item["mesafe_m"], item.get("gorev_id") or ""))
    return matches


def _borderline_calibration_rows(borderline_payload, normal_rows):
    """Yalnız temporal + lokal güçlü sınır adaylarını kalibrasyona ekler."""
    if not isinstance(borderline_payload, dict):
        return []

    candidates = []
    for region in (borderline_payload.get("bolgeler") or {}).values():
        if not isinstance(region, dict):
            continue
        for row in region.get("adaylar") or []:
            if not isinstance(row, dict):
                continue
            if not bool(row.get("sinir_temporal_lokal_guclu")):
                continue
            if bool(row.get("alarm")) or bool(row.get("saha_gorevi")):
                continue
            if any(
                _distance_m(row, normal) <= BORDERLINE_DEDUPE_RADIUS_M
                for normal in normal_rows
            ):
                continue
            item = dict(row)
            item["mikro_kaynak"] = "SPEKTRAL_SINIR_TEMPORAL_LOKAL_GUCLU"
            item["kalibrasyon_firsati"] = True
            item["alarm"] = False
            item["saha_gorevi"] = False
            candidates.append(item)

    candidates.sort(
        key=lambda item: (
            0 if item.get("gulbahce_cevre") else 1,
            _number(item.get("spektral_sinir_eksik_marj"), 999.0),
            -_number(item.get("yerel_kontrast_orani"), 0.0),
        )
    )
    return candidates[:MAX_BORDERLINE_FIELD_REVIEW]


def build_review(shortlist_payload, audit_payload, field_rows, borderline_payload=None):
    scene_dates = _scene_dates(audit_payload)
    normal_rows = [
        dict(row) for row in (shortlist_payload.get("kisa_liste") or [])
        if isinstance(row, dict)
    ]
    for row in normal_rows:
        row["mikro_kaynak"] = "NORMAL_KISA_LISTE"
        row["kalibrasyon_firsati"] = False
    borderline_rows = _borderline_calibration_rows(borderline_payload, normal_rows)
    input_rows = normal_rows + borderline_rows
    review = []
    background = []
    outcome_counts = {key: 0 for key in sorted(KNOWN_OUTCOMES)}

    for row in input_rows:
        region = str(row.get("bolge") or "")
        scene_date = scene_dates.get(region, "")
        matches = _applicable_feedback(row, scene_date, field_rows)
        updated = dict(row)
        updated["mikro_son_sentinel_tarihi"] = scene_date or None
        updated["guncel_saha_eslesmesi"] = bool(matches)
        if matches:
            nearest = matches[0]
            updated["saha_eslesme_mesafe_m"] = nearest["mesafe_m"]
            updated["saha_eslesme_gorev_id"] = nearest.get("gorev_id")
            updated["saha_eslesme_sonucu"] = nearest.get("sonuc")
            outcome_counts[nearest["sonuc"]] = outcome_counts.get(nearest["sonuc"], 0) + 1
            updated["saha_arka_plan_nedeni"] = (
                "Aynı/güncel Sentinel sahnesi için yaklaşık aynı noktada saha sonucu var; "
                "yeni mikro fırsat olarak tekrar öne çıkarılmadı. Yeni Sentinel sahnesinde "
                "hareket yeniden oluşursa otomatik olarak tekrar değerlendirilebilir."
            )
            background.append(updated)
        else:
            updated["saha_eslesme_mesafe_m"] = None
            updated["saha_eslesme_gorev_id"] = None
            updated["saha_eslesme_sonucu"] = None
            review.append(updated)

    return {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": shortlist_payload.get("ana_uretim_esigi_m2", 250),
        "mikro_aralik_m2": shortlist_payload.get("mikro_aralik_m2", [150, 249]),
        "saha_esleme_yaricapi_m": FIELD_MATCH_RADIUS_M,
        "kisa_liste_girdi": len(normal_rows),
        "sinir_temporal_lokal_guclu_girdi": len(borderline_rows),
        "toplam_kalibrasyon_girdi": len(input_rows),
        "yeni_mikro_inceleme_adayi": len(review),
        "guncel_saha_eslesmesi_arka_plan": len(background),
        "guncel_saha_eslesmesi_sonuclari": outcome_counts,
        "inceleme_adaylari": review,
        "arka_plan_saha_eslesmeleri": background,
        "not": (
            "Bu katman alarm/görev üretmez. Normal 150-249 m² kısa listeye ek olarak yalnız "
            "spektral sınır analizinde temporal + lokal birlikte güçlü bulunan en fazla iki aday "
            "kalibrasyon fırsatı olarak görülebilir. Aynı veya daha güncel saha geri bildirimi "
            "olan aday yeni fırsat gibi gösterilmez; daha yeni Sentinel sahnesi eski saha sonucunu "
            "kalıcı veto olarak kullanmaz."
        ),
    }


def _self_check():
    shortlist = {
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "kisa_liste": [
            {"bolge": "cesme", "enlem": 38.300000, "boylam": 26.400000, "alan_m2": 200},
            {"bolge": "cesme", "enlem": 38.301000, "boylam": 26.401000, "alan_m2": 200},
        ],
    }
    audit_old = {"bolgeler": {"cesme": {"son_tarih": "29.08.2026"}}}
    audit_new = {"bolgeler": {"cesme": {"son_tarih": "05.09.2026"}}}
    feedback = [
        {
            "gorev_id": "U1",
            "sonuc": "TARLA_BITKI",
            "enlem": 38.300050,
            "boylam": 26.400050,
            "son_tarih": "29.08.2026",
            "kayit_zamani": "2026-09-02 08:00 UTC",
        }
    ]
    borderline = {
        "bolgeler": {
            "cesme": {
                "adaylar": [
                    {
                        "bolge": "cesme",
                        "enlem": 38.305000,
                        "boylam": 26.405000,
                        "alan_m2": 200,
                        "sinir_temporal_lokal_guclu": True,
                        "spektral_sinir_eksik_marj": 0.01,
                        "yerel_kontrast_orani": 4.0,
                        "alarm": False,
                        "saha_gorevi": False,
                    },
                    {
                        "bolge": "cesme",
                        "enlem": 38.306000,
                        "boylam": 26.406000,
                        "alan_m2": 200,
                        "sinir_temporal_lokal_guclu": False,
                        "alarm": False,
                        "saha_gorevi": False,
                    },
                ]
            }
        }
    }
    old_result = build_review(shortlist, audit_old, feedback, borderline)
    assert old_result["kisa_liste_girdi"] == 2
    assert old_result["sinir_temporal_lokal_guclu_girdi"] == 1
    assert old_result["toplam_kalibrasyon_girdi"] == 3
    assert old_result["guncel_saha_eslesmesi_arka_plan"] == 1
    assert old_result["yeni_mikro_inceleme_adayi"] == 2
    assert old_result["arka_plan_saha_eslesmeleri"][0]["saha_eslesme_sonucu"] == "TARLA_BITKI"
    assert any(
        row.get("mikro_kaynak") == "SPEKTRAL_SINIR_TEMPORAL_LOKAL_GUCLU"
        for row in old_result["inceleme_adaylari"]
    )
    assert all(not row.get("alarm") and not row.get("saha_gorevi") for row in old_result["inceleme_adaylari"])

    new_result = build_review(shortlist, audit_new, feedback, borderline)
    assert new_result["guncel_saha_eslesmesi_arka_plan"] == 0
    assert new_result["yeni_mikro_inceleme_adayi"] == 3


def main():
    _self_check()
    if not SHORTLIST_FILE.exists() or not AUDIT_FILE.exists():
        raise RuntimeError("Mikro kısa liste veya mikro audit verisi bulunamadı.")
    shortlist_payload = json.loads(SHORTLIST_FILE.read_text(encoding="utf-8"))
    audit_payload = json.loads(AUDIT_FILE.read_text(encoding="utf-8"))
    borderline_payload = None
    if BORDERLINE_FILE.exists():
        borderline_payload = json.loads(BORDERLINE_FILE.read_text(encoding="utf-8"))
    field_rows = _read_field_rows()
    result = build_review(shortlist_payload, audit_payload, field_rows, borderline_payload)
    OUTPUT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Mikro saha geri bildirim koruması tamamlandı: "
        f"normal={result['kisa_liste_girdi']}, "
        f"sinir_guclu={result['sinir_temporal_lokal_guclu_girdi']}, "
        f"inceleme={result['yeni_mikro_inceleme_adayi']}, "
        f"saha_arka_plan={result['guncel_saha_eslesmesi_arka_plan']}. "
        "Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
