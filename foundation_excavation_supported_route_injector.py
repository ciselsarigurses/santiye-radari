"""Korunmuş 250 m²+ çoklu-kanıt temel/kepçe adayını nihai saha rotasına taşır.

Operasyonel rota koruması tarihsel/generic KONTROLE_GIT kayıtlarını filtreler ancak
pozitif-context diagnostik havuzunda ilk kez oluşan yeni bir adayı kendiliğinden
rota kaydına dönüştürmez. Bu modül bu recall boşluğunu kapatır.

Yalnız aşağıdaki mevcut kanıtlara sahip adaylar eklenebilir:
- 250 m²+,
- ``ana_esik_pozitif_destekli_diagnostik_korumali == true``,
- yüksek S2 morfoloji,
- bağımsız pozitif destek,
- tarla/bahçe, saha geri bildirimi, mevcut müşteri ve çekirdek FP-klon engeli yok,
- operasyon koridorunda (boylam <= 26.60; Gülbahçe açıkça dışarıda),
- en az iki pozitif kanıt kaynağı.

Bu katman SQLite'a yazmaz, adres/ada/parsel uydurmaz ve Sentinel-2'den gerçek derinlik
ölçtüğünü iddia etmez. Mevcut insan TEKRAR_GIT kayıtları öncelikle korunur. Rota limiti
3'tür ve 150–249 m² MİKRO adaylar asla enjekte edilmez.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import foundation_excavation_operational_route_guard as route_guard


BASE = Path(__file__).resolve().parent
ROUTE_JSON = BASE / "operational_route.json"
REPORT_JSON = BASE / "latest_report.json"
FIELD_REPORT_MD = BASE / "SAHA_RAPORU.md"
POSITIVE_CONTEXT_JSON = BASE / "foundation_excavation_positive_context_review.json"

MAIN_THRESHOLD_M2 = 250
ROUTE_LIMIT = 3
MATCH_RADIUS_M = 60.0
EAST_LIMIT_LON = 26.60


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


def _area(item):
    try:
        return float(item.get("spektral_etki_alani_m2") or item.get("alan_m2") or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_blocked(item):
    return bool(
        item.get("negatif_baglamsal_engel") is True
        or item.get("geri_bildirim_engeli") is True
        or item.get("mevcut_musteri") is True
        or item.get("tarla_bahce_baskilandi") is True
        or item.get("yanlis_pozitif_spektral_klon_baskilandi") is True
        or item.get("baseline_bitki_negatif_baglami") is True
    )


def _qualified_rows(context):
    rows = []
    for region_key, region in (context.get("bolgeler") or {}).items():
        if not isinstance(region, dict):
            continue
        scene = str(region.get("pozitif_s2_son_tarih") or "").strip()
        region_name = str(region.get("bolge") or "").strip()
        for raw in region.get("adaylar") or []:
            if not isinstance(raw, dict) or _point(raw) is None:
                continue
            lat, lon = _point(raw)
            sources = [str(x) for x in (raw.get("pozitif_kanit_kaynaklari") or []) if str(x).strip()]
            if raw.get("ana_esik_pozitif_destekli_diagnostik_korumali") is not True:
                continue
            if raw.get("morfoloji_yuksek") is not True or raw.get("bagimsiz_pozitif_destek") is not True:
                continue
            if _area(raw) < MAIN_THRESHOLD_M2 or len(set(sources)) < 2:
                continue
            if _is_blocked(raw):
                continue
            if lon > EAST_LIMIT_LON:
                continue
            # Operasyon dışı Gülbahçe adı açıkça işaretlenmişse fail-closed davran.
            if "gülbahçe" in region_name.casefold():
                continue
            rows.append(
                {
                    "region_key": str(region_key),
                    "bolge": region_name,
                    "scene": scene,
                    "raw": raw,
                    "enlem": lat,
                    "boylam": lon,
                    "alan_m2": _area(raw),
                    "morfoloji_puani": raw.get("morfoloji_puani"),
                    "sources": sources,
                }
            )
    rows.sort(
        key=lambda x: (
            float(x.get("morfoloji_puani") or 0),
            float(x.get("raw", {}).get("onset_sar_lokal_degisim_skor_db") or 0),
            float(x.get("alan_m2") or 0),
        ),
        reverse=True,
    )
    return rows


def _near_any(candidate, rows, radius=MATCH_RADIUS_M):
    point = (candidate["enlem"], candidate["boylam"])
    for row in rows:
        row_point = _point(row)
        if row_point is not None and _distance_m(point, row_point) <= radius:
            return row
    return None


def _task_id(candidate):
    seed = f"{candidate['enlem']:.6f},{candidate['boylam']:.6f},{candidate['scene']}"
    return "FK" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10].upper()


def _synthetic_task(candidate):
    raw = candidate["raw"]
    lat = candidate["enlem"]
    lon = candidate["boylam"]
    area = int(round(candidate["alan_m2"]))
    scene = candidate["scene"]
    return {
        "oncelik": "YUKSEK",
        "mahalle": None,
        "enlem": lat,
        "boylam": lon,
        "alan_m2": area,
        "sinyal": "Çoklu-kanıt temel/kepçe kazısı olasılığı · S2 morfoloji + bağımsız uydu desteği",
        "bolge": candidate["bolge"],
        "onceki_tarih": None,
        "son_tarih": scene,
        "yeni_goruntu": False,
        "harita": f"https://www.google.com/maps/dir/?api=1&destination={lat:.6f},{lon:.6f}",
        "konum_notu": (
            "Koordinat çoklu-kanıt uydu diagnostik adayının yaklaşık merkezidir. "
            "Sentinel-2 10 m veriden gerçek kazı derinliği ölçülmemiştir; saha teyidi gerekir."
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
        "onset_sar_pozitif_destek": raw.get("onset_sar_pozitif_destek") is True,
        "onset_sar_cifti": raw.get("onset_sar_cifti"),
        "onset_sar_lokal_degisim_skor_db": raw.get("onset_sar_lokal_degisim_skor_db"),
        "onset_sar_polarizasyon_uyumu": raw.get("onset_sar_polarizasyon_uyumu"),
        "onset_sar_mekansal_ayrim": raw.get("onset_sar_mekansal_ayrim"),
        "temel_derinlik_olcumu": False,
        "uydu_diagnostik_adayi": True,
        "dogrulama_durumu": "SAHA_TEYIDI_BEKLIYOR",
        "kaynak": "FOUNDATION_POSITIVE_CONTEXT",
    }


def inject(route, context, report):
    result = dict(route)
    current = [dict(x) for x in (route.get("operasyonel_rota") or []) if isinstance(x, dict)]
    report_pool = [dict(x) for x in (report.get("saha_adaylari") or []) if isinstance(x, dict)]
    qualified = _qualified_rows(context)

    injected = []
    for candidate in qualified:
        if _near_any(candidate, current) is not None:
            continue

        # Aynı nokta merkezi saha havuzunda zaten varsa yeni görev kimliği uydurmak yerine
        # mevcut kaydı zenginleştir. Yoksa yalnız kullanıcıya sunulan rotada deterministik
        # bir uydu-diagnostik kontrol kaydı oluştur; SQLite değişmez.
        existing = _near_any(candidate, report_pool)
        if existing is not None:
            task = dict(existing)
            task["temel_kazi_coklu_kanit_destekli"] = True
            task["temel_kazi_morfoloji_puani"] = candidate.get("morfoloji_puani")
            task["temel_kazi_pozitif_kanit_kaynaklari"] = candidate.get("sources")
            task["uydu_diagnostik_adayi"] = True
            task["rota_uygun"] = True
            task["saha_durumu"] = "KONTROLE_GIT"
            task["oncelik"] = "YUKSEK"
        else:
            task = _synthetic_task(candidate)
        injected.append(task)

    # İnsan TEKRAR_GIT kararı her zaman önde ve korunmuş kalır. Sonra yeni çoklu-kanıt
    # adayları gelir; kalan mevcut rota sırasını bozmadan üçlük limit doldurulur.
    repeats = [x for x in current if str(x.get("saha_durumu") or "").upper() == "TEKRAR_GIT"]
    others = [x for x in current if str(x.get("saha_durumu") or "").upper() != "TEKRAR_GIT"]
    final = []
    for row in repeats + injected + others:
        if len(final) >= ROUTE_LIMIT:
            break
        point = _point(row)
        if point is not None and any(
            _point(prev) is not None and _distance_m(point, _point(prev)) <= MATCH_RADIUS_M
            for prev in final
        ):
            continue
        final.append(row)

    for index, row in enumerate(final, start=1):
        row["gunluk_sira"] = index

    result["operasyonel_rota"] = final
    result["operasyon_odak_sonrasi_rota_gorevleri"] = [str(x.get("gorev_id") or "") for x in final]
    result["temel_kazi_kapisi_sonrasi_rota_gorevleri"] = [str(x.get("gorev_id") or "") for x in final]
    result["temel_kazi_destekli_yeni_rota_adayi_sayi"] = len(injected)
    result["temel_kazi_destekli_yeni_rota_adaylari"] = [
        {
            "gorev_id": x.get("gorev_id"),
            "enlem": x.get("enlem"),
            "boylam": x.get("boylam"),
            "alan_m2": x.get("alan_m2"),
            "morfoloji_puani": x.get("temel_kazi_morfoloji_puani"),
            "pozitif_kanit_kaynaklari": x.get("temel_kazi_pozitif_kanit_kaynaklari"),
        }
        for x in injected
    ]
    result["temel_kazi_destekli_rota_enjeksiyonu"] = True
    result["temel_kazi_destekli_rota_enjeksiyon_notu"] = (
        "Yalnız 250 m²+ korunmuş çoklu-pozitif temel/kepçe adayları, tarla/FP/müşteri "
        "engelleri geçtikten sonra nihai kullanıcı rotasına eklenebilir. MİKRO adaylar "
        "diagnostik kalır; SQLite görev havuzu değiştirilmez; Sentinel-2'den gerçek derinlik ölçülmez."
    )
    return result


def _self_check():
    context = {
        "bolgeler": {
            "cesme": {
                "bolge": "Çeşme merkez · Alaçatı · Ilıca",
                "pozitif_s2_son_tarih": "18.09.2026",
                "adaylar": [
                    {
                        "enlem": 38.307849,
                        "boylam": 26.333503,
                        "spektral_etki_alani_m2": 400,
                        "morfoloji_puani": 95,
                        "morfoloji_yuksek": True,
                        "bagimsiz_pozitif_destek": True,
                        "pozitif_kanit_kaynaklari": ["S2_MORFOLOJI", "S1_ONSET_11_17"],
                        "ana_esik_pozitif_destekli_diagnostik_korumali": True,
                        "negatif_baglamsal_engel": False,
                        "geri_bildirim_engeli": False,
                        "mevcut_musteri": False,
                        "tarla_bahce_baskilandi": False,
                        "yanlis_pozitif_spektral_klon_baskilandi": False,
                        "baseline_bitki_negatif_baglami": False,
                        "onset_sar_pozitif_destek": True,
                        "onset_sar_cifti": "2026-09-11->2026-09-17",
                        "onset_sar_lokal_degisim_skor_db": 2.817,
                        "onset_sar_polarizasyon_uyumu": "CIFT_POL_GUCLU",
                        "onset_sar_mekansal_ayrim": "KOMPAKT_LOKAL_DESTEKLI",
                    },
                    {
                        "enlem": 38.31,
                        "boylam": 26.34,
                        "spektral_etki_alani_m2": 200,
                        "morfoloji_puani": 100,
                        "morfoloji_yuksek": True,
                        "bagimsiz_pozitif_destek": True,
                        "pozitif_kanit_kaynaklari": ["S2_MORFOLOJI", "S1_ONSET_11_17"],
                        "ana_esik_pozitif_destekli_diagnostik_korumali": False,
                    },
                ],
            }
        }
    }
    payload = inject({"operasyonel_rota": []}, context, {"saha_adaylari": []})
    assert len(payload["operasyonel_rota"]) == 1, payload
    row = payload["operasyonel_rota"][0]
    assert row["enlem"] == 38.307849
    assert row["saha_durumu"] == "KONTROLE_GIT"
    assert row["uydu_diagnostik_adayi"] is True
    assert row["temel_derinlik_olcumu"] is False

    blocked = json.loads(json.dumps(context))
    blocked["bolgeler"]["cesme"]["adaylar"][0]["tarla_bahce_baskilandi"] = True
    payload = inject({"operasyonel_rota": []}, blocked, {"saha_adaylari": []})
    assert payload["operasyonel_rota"] == [], payload

    east = json.loads(json.dumps(context))
    east["bolgeler"]["cesme"]["adaylar"][0]["boylam"] = 26.65
    payload = inject({"operasyonel_rota": []}, east, {"saha_adaylari": []})
    assert payload["operasyonel_rota"] == [], payload

    repeat = {
        "gorev_id": "HUMAN",
        "saha_durumu": "TEKRAR_GIT",
        "enlem": 38.32,
        "boylam": 26.32,
    }
    payload = inject({"operasyonel_rota": [repeat]}, context, {"saha_adaylari": []})
    assert payload["operasyonel_rota"][0]["gorev_id"] == "HUMAN"
    assert payload["operasyonel_rota"][1]["uydu_diagnostik_adayi"] is True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("foundation excavation supported route injector self-check: ok")
        return

    route = _load(ROUTE_JSON)
    report = _load(REPORT_JSON)
    context = _load(POSITIVE_CONTEXT_JSON)
    enriched = inject(route, context, report)
    ROUTE_JSON.write_text(json.dumps(enriched, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    updated_report = route_guard._update_report(report, enriched)
    REPORT_JSON.write_text(json.dumps(updated_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if FIELD_REPORT_MD.exists():
        text = FIELD_REPORT_MD.read_text(encoding="utf-8")
        FIELD_REPORT_MD.write_text(
            route_guard._update_markdown(text, enriched.get("operasyonel_rota") or []),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
