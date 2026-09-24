"""Yüksek güvenli post-onset SAR süreklilik adayını nihai saha rotasına taşır.

Bu katman yalnız 13 Eylül sakin baseline -> kullanılabilir post-onset Sentinel-2
morfolojisi ile, post-onset S1 süreklilik diagnostiğinde birlikte güçlü kalan
250 m²+ adayları ele alır. Sentinel-1 çifti kazının başlangıcını çevrelemiyorsa
"onset" kanıtı sayılmaz; yalnız devam eden müdahale için bağımsız ikinci kanıttır.

Kurallar:
- 250 m² ana eşik korunur; MİKRO enjekte edilmez.
- yüksek S2 morfoloji + >=2 dB S1 + çift-pol güçlü + kompakt-lokal gerekir.
- SAR çifti post-onset S2 tarihini çevrelemelidir.
- tarla/bahçe ve saha geri bildirimi engelleri korunur.
- güncel manual_field_feedback içinde YANLIS_POZITIF veya MEVCUT_MUSTERI olan
  noktalar ayrıca fail-closed bastırılır.
- Gülbahçe operasyonel öncelik değildir; doğu koridor sınırı (>26.60) dışarıda kalır.
- adres/ada/parsel/hukuki statü üretilmez; gerçek kazı derinliği ölçülmez.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

import foundation_excavation_operational_route_guard as route_guard


BASE = Path(__file__).resolve().parent
ROUTE_JSON = BASE / "operational_route.json"
REPORT_JSON = BASE / "latest_report.json"
FIELD_REPORT_MD = BASE / "SAHA_RAPORU.md"
PERSISTENCE_JSON = BASE / "post_onset_sar_persistence_review.json"
FEEDBACK_JSON = BASE / "manual_field_feedback.json"

MAIN_THRESHOLD_M2 = 250
ROUTE_LIMIT = 3
MATCH_RADIUS_M = 60.0
FEEDBACK_RADIUS_M = 30.0
EAST_LIMIT_LON = 26.60
SAR_STRONG_DB = 2.0
ONSET_START = datetime.strptime("2026-09-15", "%Y-%m-%d").date()


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
    mean_lat = math.radians((float(a[0]) + float(b[0])) / 2.0)
    north = (float(a[0]) - float(b[0])) * 110570
    east = (float(a[1]) - float(b[1])) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _date(value):
    text = str(value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _area(item):
    try:
        return float(item.get("spektral_etki_alani_m2") or item.get("alan_m2") or 0)
    except (TypeError, ValueError):
        return 0.0


def _near(candidate, rows, radius=MATCH_RADIUS_M):
    point = _point(candidate)
    if point is None:
        return None
    for row in rows:
        row_point = _point(row)
        if row_point is not None and _distance_m(point, row_point) <= radius:
            return row
    return None


def _feedback_block(candidate, feedback):
    point = _point(candidate)
    if point is None:
        return True, None
    for row in feedback.get("kayitlar") or []:
        if not isinstance(row, dict):
            continue
        row_point = _point(row)
        if row_point is None:
            continue
        sonuc = str(row.get("sonuc") or "").upper()
        if sonuc not in {"YANLIS_POZITIF", "MEVCUT_MUSTERI"}:
            continue
        try:
            radius = max(FEEDBACK_RADIUS_M, float(row.get("eslesme_yaricapi_m") or 0))
        except (TypeError, ValueError):
            radius = FEEDBACK_RADIUS_M
        if _distance_m(point, row_point) <= radius:
            return True, row
    return False, None


def _qualified_rows(persistence, feedback):
    rows = []
    for region_key, region in (persistence.get("bolgeler") or {}).items():
        if not isinstance(region, dict):
            continue

        baseline_text = str(region.get("baseline_tarih") or "").strip()
        post_text = str(region.get("post_onset_tarih") or "").strip()
        baseline_date = _date(baseline_text)
        post_date = _date(post_text)
        if baseline_date is None or post_date is None or not baseline_date < post_date:
            continue

        region_name = str(region.get("bolge") or region_key).strip()
        for raw in region.get("adaylar") or []:
            if not isinstance(raw, dict) or _point(raw) is None:
                continue
            lat, lon = _point(raw)

            if _area(raw) < MAIN_THRESHOLD_M2:
                continue
            if raw.get("ana_esik") is not True:
                continue
            if raw.get("yuksek_guven_diagnostik") is not True:
                continue
            if raw.get("morfoloji_yuksek") is not True:
                continue
            if raw.get("sar_post_onset_sureklilik_destek") is not True:
                continue
            if raw.get("tarla_bahce_engeli") is True or raw.get("geri_bildirim_engeli") is True:
                continue

            try:
                sar_score = float(raw.get("sar_lokal_degisim_skor_db") or 0)
            except (TypeError, ValueError):
                sar_score = 0.0
            if sar_score < SAR_STRONG_DB:
                continue
            if str(raw.get("sar_polarizasyon_uyumu") or "") != "CIFT_POL_GUCLU":
                continue
            if str(raw.get("sar_mekansal_ayrim") or "") != "KOMPAKT_LOKAL_DESTEKLI":
                continue

            sar_old = _date(raw.get("sar_eski_tarih"))
            sar_new = _date(raw.get("sar_yeni_tarih"))
            if (
                sar_old is None
                or sar_new is None
                or sar_old < ONSET_START
                or not sar_old <= post_date <= sar_new
                or not sar_old < sar_new
            ):
                continue

            blocked, feedback_row = _feedback_block(raw, feedback)
            if blocked:
                continue

            if lon > EAST_LIMIT_LON:
                continue
            mahalle = str(raw.get("mahalle") or "").casefold()
            if mahalle == "gülbahçe":
                continue

            rows.append(
                {
                    "region_key": str(region_key),
                    "bolge": region_name,
                    "scene": post_text,
                    "s2_onceki_tarih": baseline_text,
                    "raw": raw,
                    "enlem": lat,
                    "boylam": lon,
                    "alan_m2": _area(raw),
                    "morfoloji_puani": raw.get("morfoloji_puani"),
                    "sar_skor_db": sar_score,
                    "sources": [
                        "S2_BASELINE_MORFOLOJI",
                        "S1_POST_ONSET_PERSISTENCE",
                    ],
                    "feedback_engeli": feedback_row,
                }
            )

    rows.sort(
        key=lambda x: (
            float(x.get("morfoloji_puani") or 0),
            float(x.get("sar_skor_db") or 0),
            float(x.get("alan_m2") or 0),
        ),
        reverse=True,
    )
    return rows


def _task_id(candidate):
    seed = (
        f"{candidate['enlem']:.6f},{candidate['boylam']:.6f},"
        f"{candidate['s2_onceki_tarih']}->{candidate['scene']}"
    )
    return "FKP" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:9].upper()


def _synthetic_task(candidate):
    raw = candidate["raw"]
    lat = candidate["enlem"]
    lon = candidate["boylam"]
    baseline = candidate["s2_onceki_tarih"]
    post = candidate["scene"]
    area = int(round(candidate["alan_m2"]))
    return {
        "oncelik": "YUKSEK",
        "mahalle": None,
        "enlem": lat,
        "boylam": lon,
        "alan_m2": area,
        "sinyal": (
            "Çoklu-kanıt temel/kepçe kazısı olasılığı · "
            "S2 baseline morfoloji + post-onset S1 süreklilik"
        ),
        "bolge": candidate["bolge"],
        "yeni_goruntu": False,
        "harita": (
            "https://www.google.com/maps/dir/?api=1&destination="
            f"{lat:.6f},{lon:.6f}"
        ),
        "konum_notu": (
            "Koordinat yüksek güvenli uydu diagnostik adayının yaklaşık merkezidir. "
            "Sentinel-2 10 m veriden gerçek kazı derinliği ölçülmemiştir. "
            "S1 kanıtı kazının başlangıç tarihini değil post-onset sürekliliğini destekler; "
            "saha teyidi gerekir."
        ),
        "gorev_id": _task_id(candidate),
        "saha_durumu": "KONTROLE_GIT",
        "takip_gorevi": False,
        "gecikmis": False,
        "adres": None,
        "ada": None,
        "parsel": None,
        "firma": None,
        "proje": None,
        "rota_uygun": True,
        "temel_kazi_coklu_kanit_destekli": True,
        "temel_kazi_morfoloji_puani": raw.get("morfoloji_puani"),
        "temel_kazi_pozitif_kanit_kaynaklari": candidate["sources"],
        "onset_sar_pozitif_destek": False,
        "sar_onset_baslangici_kaniti": False,
        "post_onset_sar_sureklilik_destek": True,
        "post_onset_sar_cifti": (
            f"{raw.get('sar_eski_tarih')}->{raw.get('sar_yeni_tarih')}"
        ),
        "post_onset_sar_lokal_degisim_skor_db": raw.get("sar_lokal_degisim_skor_db"),
        "post_onset_sar_polarizasyon_uyumu": raw.get("sar_polarizasyon_uyumu"),
        "post_onset_sar_mekansal_ayrim": raw.get("sar_mekansal_ayrim"),
        "temel_derinlik_olcumu": False,
        "uydu_diagnostik_adayi": True,
        "dogrulama_durumu": "SAHA_TEYIDI_BEKLIYOR",
        "kaynak": "POST_ONSET_SAR_PERSISTENCE",
        "onceki_tarih": baseline,
        "son_tarih": post,
        "s2_morfoloji_onceki_tarih": baseline,
        "s2_morfoloji_son_tarih": post,
        "s2_morfoloji_cifti": f"{baseline}->{post}",
    }


def inject(route, persistence, feedback):
    result = dict(route)
    current = [
        dict(x)
        for x in (route.get("operasyonel_rota") or [])
        if isinstance(x, dict)
    ]
    qualified = _qualified_rows(persistence, feedback)

    injected = []
    for candidate in qualified:
        if _near(candidate, current) is not None:
            continue
        task = _synthetic_task(candidate)
        injected.append(task)

    repeats = [
        x
        for x in current
        if str(x.get("saha_durumu") or "").upper() == "TEKRAR_GIT"
    ]
    others = [
        x
        for x in current
        if str(x.get("saha_durumu") or "").upper() != "TEKRAR_GIT"
    ]

    final = []
    for row in repeats + injected + others:
        if len(final) >= ROUTE_LIMIT:
            break
        if _near(row, final) is not None:
            continue
        final.append(row)

    for index, row in enumerate(final, start=1):
        row["gunluk_sira"] = index

    result["operasyonel_rota"] = final
    result["operasyon_odak_sonrasi_rota_gorevleri"] = [
        str(x.get("gorev_id") or "") for x in final
    ]
    result["temel_kazi_kapisi_sonrasi_rota_gorevleri"] = [
        str(x.get("gorev_id") or "") for x in final
    ]
    result["post_onset_persistence_yeni_rota_adayi_sayi"] = len(injected)
    result["post_onset_persistence_yeni_rota_adaylari"] = [
        {
            "gorev_id": x.get("gorev_id"),
            "enlem": x.get("enlem"),
            "boylam": x.get("boylam"),
            "alan_m2": x.get("alan_m2"),
            "morfoloji_puani": x.get("temel_kazi_morfoloji_puani"),
            "s2_morfoloji_cifti": x.get("s2_morfoloji_cifti"),
            "post_onset_sar_cifti": x.get("post_onset_sar_cifti"),
            "post_onset_sar_lokal_degisim_skor_db": x.get(
                "post_onset_sar_lokal_degisim_skor_db"
            ),
            "pozitif_kanit_kaynaklari": x.get(
                "temel_kazi_pozitif_kanit_kaynaklari"
            ),
        }
        for x in injected
    ]
    result["post_onset_persistence_rota_enjeksiyonu"] = True
    result["post_onset_persistence_rota_notu"] = (
        "Yalnız 250 m²+ yüksek S2 morfolojisi ile >=2 dB, çift-pol güçlü ve "
        "kompakt-lokal post-onset S1 sürekliliği birlikte bulunan adaylar eklenir. "
        "S1 sürekliliği onset başlangıç kanıtı sayılmaz; MİKRO adaylar diagnostik kalır."
    )
    return result


def _self_check():
    persistence = {
        "bolgeler": {
            "cesme": {
                "bolge": "Çeşme merkez · Alaçatı · Ilıca",
                "baseline_tarih": "13.09.2026",
                "post_onset_tarih": "18.09.2026",
                "adaylar": [
                    {
                        "enlem": 38.278725,
                        "boylam": 26.303853,
                        "spektral_etki_alani_m2": 500,
                        "morfoloji_puani": 100,
                        "morfoloji_yuksek": True,
                        "sar_eski_tarih": "2026-09-17",
                        "sar_yeni_tarih": "2026-09-23",
                        "sar_lokal_degisim_skor_db": 3.776,
                        "sar_polarizasyon_uyumu": "CIFT_POL_GUCLU",
                        "sar_mekansal_ayrim": "KOMPAKT_LOKAL_DESTEKLI",
                        "sar_post_onset_sureklilik_destek": True,
                        "sar_onset_baslangici_kaniti": False,
                        "tarla_bahce_engeli": False,
                        "geri_bildirim_engeli": False,
                        "ana_esik": True,
                        "yuksek_guven_diagnostik": True,
                    },
                    {
                        "enlem": 38.31,
                        "boylam": 26.31,
                        "spektral_etki_alani_m2": 200,
                        "morfoloji_puani": 100,
                        "morfoloji_yuksek": True,
                        "sar_eski_tarih": "2026-09-17",
                        "sar_yeni_tarih": "2026-09-23",
                        "sar_lokal_degisim_skor_db": 4.0,
                        "sar_polarizasyon_uyumu": "CIFT_POL_GUCLU",
                        "sar_mekansal_ayrim": "KOMPAKT_LOKAL_DESTEKLI",
                        "sar_post_onset_sureklilik_destek": True,
                        "tarla_bahce_engeli": False,
                        "geri_bildirim_engeli": False,
                        "ana_esik": False,
                        "yuksek_guven_diagnostik": False,
                    },
                ],
            }
        }
    }
    feedback = {"kayitlar": []}

    payload = inject({"operasyonel_rota": []}, persistence, feedback)
    assert len(payload["operasyonel_rota"]) == 1, payload
    row = payload["operasyonel_rota"][0]
    assert row["enlem"] == 38.278725
    assert row["onceki_tarih"] == "13.09.2026"
    assert row["son_tarih"] == "18.09.2026"
    assert row["s2_morfoloji_cifti"] == "13.09.2026->18.09.2026"
    assert row["sar_onset_baslangici_kaniti"] is False
    assert row["post_onset_sar_sureklilik_destek"] is True
    assert row["temel_derinlik_olcumu"] is False

    weak = json.loads(json.dumps(persistence))
    weak["bolgeler"]["cesme"]["adaylar"][0]["sar_lokal_degisim_skor_db"] = 1.99
    assert inject({"operasyonel_rota": []}, weak, feedback)["operasyonel_rota"] == []

    field = json.loads(json.dumps(persistence))
    field["bolgeler"]["cesme"]["adaylar"][0]["tarla_bahce_engeli"] = True
    assert inject({"operasyonel_rota": []}, field, feedback)["operasyonel_rota"] == []

    customer_feedback = {
        "kayitlar": [
            {
                "enlem": 38.278725,
                "boylam": 26.303853,
                "eslesme_yaricapi_m": 30,
                "sonuc": "MEVCUT_MUSTERI",
            }
        ]
    }
    assert (
        inject({"operasyonel_rota": []}, persistence, customer_feedback)[
            "operasyonel_rota"
        ]
        == []
    )

    bad_time = json.loads(json.dumps(persistence))
    bad_time["bolgeler"]["cesme"]["adaylar"][0]["sar_eski_tarih"] = "2026-09-19"
    assert inject({"operasyonel_rota": []}, bad_time, feedback)["operasyonel_rota"] == []

    repeat = {
        "gorev_id": "HUMAN",
        "saha_durumu": "TEKRAR_GIT",
        "enlem": 38.32,
        "boylam": 26.32,
    }
    payload = inject({"operasyonel_rota": [repeat]}, persistence, feedback)
    assert payload["operasyonel_rota"][0]["gorev_id"] == "HUMAN"
    assert payload["operasyonel_rota"][1]["kaynak"] == "POST_ONSET_SAR_PERSISTENCE"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if args.check_only:
        print("post-onset SAR persistence route guard self-check: ok")
        return

    route = _load(ROUTE_JSON)
    persistence = _load(PERSISTENCE_JSON)
    feedback = _load(FEEDBACK_JSON)
    enriched = inject(route, persistence, feedback)
    ROUTE_JSON.write_text(
        json.dumps(enriched, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    report = _load(REPORT_JSON)
    updated_report = route_guard._update_report(report, enriched)
    REPORT_JSON.write_text(
        json.dumps(updated_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if FIELD_REPORT_MD.exists():
        text = FIELD_REPORT_MD.read_text(encoding="utf-8")
        FIELD_REPORT_MD.write_text(
            route_guard._update_markdown(
                text,
                enriched.get("operasyonel_rota") or [],
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
