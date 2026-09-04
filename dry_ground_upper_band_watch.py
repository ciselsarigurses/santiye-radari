"""900-2.000 m² kuru-zemin adaylarının sessizce karar hattı dışında kalmasını ölçer.

Ana Sentinel üretim eşiği 250 m² ve 15 Eylül sonrası kuru-zemin DOĞRULAMA geçidinin
mevcut 250-900 m² sınırı değiştirilmez. Bu katman yalnız ``dry_ground_temporal_audit``
içinde 900 m² üstünde kalan, izole/non-lineer geometri + uzun-temporal ani başlangıç +
kararlı geçmiş zemin + güvenli yörünge kanıtlarını birlikte taşıyan adayları sayar.

Amaç 900 m² sınırının gerçek erken hafriyat sinyalini sessizce dışarıda bırakıp
bırakmadığını yasak döneminde ölçmektir. 5x5 yerellik kanıtı bu üst bant için henüz
üretilmediğinden bu kayıtlar alarm, saha görevi veya 15 Eylül DOĞRULAMA adayı değildir.
Ana saha kuyruğuna 40 m içindeki mevcut görevler ayrıca işaretlenir; böylece sadece
zaten kapsanan noktalar yüzünden yeni bir politika ihtiyacı varmış gibi görünmez.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


BASE = Path(__file__).resolve().parent
TEMPORAL_AUDIT = BASE / "dry_ground_temporal_audit.json"
REPORT_JSON = BASE / "latest_report.json"
OUTPUT_JSON = BASE / "dry_ground_upper_band_watch.json"
ISTANBUL = ZoneInfo("Europe/Istanbul")

LOWER_EXCLUSIVE_M2 = 900
UPPER_INCLUSIVE_M2 = 2_000
DUPLICATE_RADIUS_M = 40.0


def _load(path):
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


def _distance_m(first, second):
    try:
        lat1 = float(first.get("enlem"))
        lon1 = float(first.get("boylam"))
        lat2 = float(second.get("enlem"))
        lon2 = float(second.get("boylam"))
    except (TypeError, ValueError, AttributeError):
        return float("inf")
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    north = (lat1 - lat2) * 110_570.0
    east = (lon1 - lon2) * 111_320.0 * math.cos(mean_lat)
    return math.hypot(north, east)


def _strong_temporal(item):
    area = _number(item.get("alan_m2"))
    return bool(
        LOWER_EXCLUSIVE_M2 < area <= UPPER_INCLUSIVE_M2
        and item.get("ani_baslangic_destegi") is True
        and item.get("istikrarsiz_zemin_riski") is False
        and item.get("uzun_temporal_istikrarsiz_zemin_riski") is False
        and str(item.get("uzun_temporal_koruma") or "").upper() == "KORUNDU"
        and item.get("yorunge_geometri_riski") is False
        and item.get("izole_saha_benzeri") is True
        and item.get("lineer_geometri_riski") is False
    )


def _near_main_task(item, report):
    distances = [
        _distance_m(item, task)
        for task in (report or {}).get("saha_adaylari") or []
        if isinstance(task, dict)
    ]
    nearest = min(distances) if distances else float("inf")
    return nearest <= DUPLICATE_RADIUS_M, nearest


def build_watch(temporal, report):
    if not isinstance(temporal, dict) or not isinstance(report, dict):
        return None
    report_date = str(report.get("rapor_tarihi") or "")
    if not report_date or str(temporal.get("rapor_tarihi") or "") != report_date:
        return None

    candidates = []
    region_summary = {}
    for region_key, region in (temporal.get("bolgeler") or {}).items():
        if not isinstance(region, dict) or region.get("durum") != "ok":
            continue
        rows = []
        for raw in region.get("adaylar") or []:
            if not isinstance(raw, dict) or not _strong_temporal(raw):
                continue
            near, nearest = _near_main_task(raw, report)
            lat = round(_number(raw.get("enlem")), 6)
            lon = round(_number(raw.get("boylam")), 6)
            rows.append(
                {
                    "mahalle": str(raw.get("mahalle") or "Mevki doğrulanmadı"),
                    "enlem": lat,
                    "boylam": lon,
                    "alan_m2": round(_number(raw.get("alan_m2"))),
                    "son_item": str(region.get("son_item") or ""),
                    "son_tarih": region.get("son_tarih"),
                    "son_cift_bsi_degisim": round(_number(raw.get("son_cift_bsi_degisim")), 4),
                    "uzun_temporal_ani_baslangic_orani": round(
                        _number(raw.get("uzun_temporal_ani_baslangic_orani")), 3
                    ),
                    "ana_goreve_40m_yakin": bool(near),
                    "en_yakin_ana_gorev_m": None if math.isinf(nearest) else round(nearest, 1),
                    "yerellik_5x5_durumu": "UST_BANT_ICIN_HENUZ_OLCULMEDI",
                    "alarm": False,
                    "saha_gorevi": False,
                    "harita": f"https://www.google.com/maps/dir/?api=1&destination={lat:.6f},{lon:.6f}",
                }
            )
        rows.sort(
            key=lambda item: (
                bool(item.get("ana_goreve_40m_yakin")),
                -_number(item.get("uzun_temporal_ani_baslangic_orani")),
                -_number(item.get("son_cift_bsi_degisim")),
                _number(item.get("alan_m2")),
            )
        )
        candidates.extend({**row, "bolge": str(region.get("bolge") or region_key)} for row in rows)
        region_summary[region_key] = {
            "bolge": str(region.get("bolge") or region_key),
            "son_item": str(region.get("son_item") or ""),
            "guclu_temporal_ust_bant": len(rows),
            "ana_gorevle_zaten_kapsanan": sum(1 for row in rows if row.get("ana_goreve_40m_yakin")),
            "yerellik_testi_gereken": sum(1 for row in rows if not row.get("ana_goreve_40m_yakin")),
        }

    candidates.sort(
        key=lambda item: (
            bool(item.get("ana_goreve_40m_yakin")),
            -_number(item.get("uzun_temporal_ani_baslangic_orani")),
            -_number(item.get("son_cift_bsi_degisim")),
            _number(item.get("alan_m2")),
        )
    )
    uncovered = [item for item in candidates if not item.get("ana_goreve_40m_yakin")]
    return {
        "rapor_tarihi": report_date,
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "amac": (
            "Mevcut 250-900 m² kuru-zemin teyit geçidinin üstünde kalan 901-2.000 m² "
            "güçlü temporal/izole adayları yalnız kalibrasyon için görünür kılmak."
        ),
        "ana_uretim_esigi_m2": 250,
        "mevcut_kuru_zemin_dogrulama_ust_siniri_m2": LOWER_EXCLUSIVE_M2,
        "izlenen_ust_bant_m2": [LOWER_EXCLUSIVE_M2 + 1, UPPER_INCLUSIVE_M2],
        "alarm": False,
        "saha_gorevi": False,
        "operasyonel": False,
        "yerellik_5x5_olculmeden_15eylul_rotasina_giremez": True,
        "bolgeler": region_summary,
        "guclu_temporal_ust_bant_toplam": len(candidates),
        "ana_gorev_disinda_yerellik_testi_gereken": len(uncovered),
        "adaylar": candidates,
    }


def _stable_render(payload):
    stable = dict(payload)
    stable.pop("olusturma", None)
    return json.dumps(stable, ensure_ascii=False, indent=2, sort_keys=True)


def write_watch():
    payload = build_watch(_load(TEMPORAL_AUDIT), _load(REPORT_JSON))
    if payload is None:
        raise RuntimeError("Üst bant izleme için temporal audit ve günlük rapor aynı güne ait değil.")
    before = _load(OUTPUT_JSON)
    changed = before is None or _stable_render(before) != _stable_render(payload)
    if changed:
        OUTPUT_JSON.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return changed, payload


def _self_check():
    strong = {
        "mahalle": "Ilıca",
        "enlem": 38.30,
        "boylam": 26.35,
        "alan_m2": 1000,
        "ani_baslangic_destegi": True,
        "istikrarsiz_zemin_riski": False,
        "uzun_temporal_istikrarsiz_zemin_riski": False,
        "uzun_temporal_koruma": "KORUNDU",
        "yorunge_geometri_riski": False,
        "izole_saha_benzeri": True,
        "lineer_geometri_riski": False,
        "uzun_temporal_ani_baslangic_orani": 10.0,
        "son_cift_bsi_degisim": 0.15,
    }
    assert _strong_temporal(strong)
    assert not _strong_temporal({**strong, "alan_m2": 900})
    assert not _strong_temporal({**strong, "alan_m2": 2100})
    assert not _strong_temporal({**strong, "lineer_geometri_riski": True})

    temporal = {
        "rapor_tarihi": "2026-09-04",
        "bolgeler": {"cesme": {"durum": "ok", "bolge": "Çeşme", "son_item": "S2", "son_tarih": "03.09.2026", "adaylar": [strong]}},
    }
    report = {"rapor_tarihi": "2026-09-04", "saha_adaylari": []}
    payload = build_watch(temporal, report)
    assert payload and payload["guclu_temporal_ust_bant_toplam"] == 1
    assert payload["ana_gorev_disinda_yerellik_testi_gereken"] == 1
    assert payload["adaylar"][0]["alarm"] is False
    assert payload["ana_uretim_esigi_m2"] == 250

    covered = {**report, "saha_adaylari": [{"enlem": 38.3001, "boylam": 26.3501}]}
    covered_payload = build_watch(temporal, covered)
    assert covered_payload and covered_payload["ana_gorev_disinda_yerellik_testi_gereken"] == 0
    print("dry_ground_upper_band_watch self-check: OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        return 0
    changed, payload = write_watch()
    print(
        "Kuru zemin üst bant izleme: "
        f"güçlü={payload['guclu_temporal_ust_bant_toplam']}, "
        f"yerellik_testi_gereken={payload['ana_gorev_disinda_yerellik_testi_gereken']}, "
        f"dosya_değişti={changed}. Alarm/görev üretilmedi."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
