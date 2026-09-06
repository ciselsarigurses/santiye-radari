"""Spektral sınır MİKRO analizini yalnız güncel Sentinel 250+ bağlamıyla çalıştırır.

`micro_spectral_borderline_guard.py` ana karar mantığını korur. Bu ince runner yalnız
250+ yakınlık referansını MİKRO taramasının kullandığı güncel Sentinel tarihleriyle
sınırlar. Böylece günler önce açık kalmış saha backlog'u, bugünkü 150-249 m² kompakt
bir near-miss sinyalini yanlışlıkla arka plana itemez.

Ana 250 m² üretim eşiği, normal MİKRO spektral kapıları ve alarm/görev kuralları
değişmez. Çıktı yalnız diagnostiktir.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import micro_site_shortlist as shortlist
import micro_spectral_borderline_guard as guard


RAW_FILE = Path(__file__).with_name("micro_site_audit.json")
OUTPUT_FILE = Path(__file__).with_name("micro_spectral_borderline_review.json")


def _production_context(raw_payload, production_rows=None):
    source_dates = shortlist._current_source_dates(raw_payload)
    if production_rows is None:
        production_all = shortlist._production_candidates()
    else:
        production_all = [row for row in production_rows if isinstance(row, dict)]
    production_current = shortlist._filter_current_production(
        production_all,
        source_dates,
    )
    return source_dates, production_all, production_current


def _candidate_pool(raw_payload, production_rows=None):
    raw_rows = shortlist._raw_candidates(raw_payload)
    deduped, _ = shortlist._dedupe(raw_rows)
    source_dates, production_all, production_current = _production_context(
        raw_payload,
        production_rows=production_rows,
    )
    annotated = shortlist._annotate(deduped, production_current)

    selected = []
    for row in annotated:
        reason = guard._borderline_reason(row)
        if reason is None:
            continue
        item = dict(row)
        item["spektral_sinir_inceleme"] = True
        item["spektral_sinir_metrigi"] = reason["metrik"]
        item["spektral_sinir_eksik_marj"] = reason["eksik_marj"]
        item["spektral_sinir_esik"] = reason["esik"]
        item["spektral_sinir_deger"] = reason["deger"]
        item["alarm"] = False
        item["saha_gorevi"] = False
        selected.append(item)

    selected.sort(
        key=lambda item: (
            guard._number(item.get("spektral_sinir_eksik_marj"), 999.0),
            -shortlist._strength(item),
            -guard._number(item.get("doluluk_orani"), 0.0),
        )
    )
    context = {
        "yakinlik_referans_sentinel_tarihleri": sorted(source_dates),
        "250plus_yakinlik_referans_toplam_saha_adayi": len(production_all),
        "250plus_yakinlik_referans_guncel_sentinel": len(production_current),
        "250plus_yakinlik_referans_dislanan_eski_veya_250alti": (
            len(production_all) - len(production_current)
        ),
    }
    return selected[: guard.MAX_REVIEW_CANDIDATES], context


def build_review(raw_payload, production_rows=None):
    rows, context = _candidate_pool(raw_payload, production_rows=production_rows)

    # Ana guard'ın temporal/lokal sınıflandırmasını değiştirmeden yalnız aday havuzunu
    # güncel Sentinel bağlamıyla besle. Değişiklik bu runner içinde geri alınabilir.
    original_candidate_pool = guard._candidate_pool
    guard._candidate_pool = lambda _payload: rows
    try:
        result = guard.build_review(raw_payload)
    finally:
        guard._candidate_pool = original_candidate_pool

    result["kaynak_mikro_olusturma"] = raw_payload.get("olusturma")
    result.update(context)
    result["baglam_notu"] = (
        "Spektral sınır yakınlık filtresi yalnız MİKRO taramasının güncel Sentinel "
        "tarihindeki 250+ adayları kullanır; eski açık saha backlog'u near-miss MİKRO "
        "sinyalini bastırmaz. Alarm/görev/250 m² eşiği değişmez."
    )
    return result


def _self_check():
    guard._self_check()
    raw = {
        "olusturma": "2026-09-06 15:39 +0300",
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "son_tarih": "05.09.2026",
                "adaylar": [
                    {
                        "bolge": "cesme",
                        "enlem": 38.30,
                        "boylam": 26.40,
                        "alan_m2": 200,
                        "doluluk_orani": 1.0,
                        "ortalama_rgb_degisim": 0.30,
                        "ortalama_ndvi_kaybi": 0.21,
                        "ortalama_parlaklik_artisi": 0.30,
                    }
                ],
            },
            "uzunkuyu": {
                "durum": "ok",
                "son_tarih": "05.09.2026",
                "adaylar": [],
            },
        },
    }
    stale_reference = {
        "enlem": 38.30,
        "boylam": 26.40,
        "alan_m2": 400,
        "son_tarih": "26.08.2026",
    }
    current_reference = dict(stale_reference, son_tarih="05.09.2026")

    stale_rows, stale_context = _candidate_pool(raw, [stale_reference])
    assert len(stale_rows) == 1, (
        "Eski 250+ backlog güncel spektral sınır MİKRO adayını bastırmamalıdır."
    )
    assert stale_context["250plus_yakinlik_referans_guncel_sentinel"] == 0

    current_rows, current_context = _candidate_pool(raw, [current_reference])
    assert len(current_rows) == 0, (
        "Aynı güncel Sentinel tarihindeki yakın 250+ aday MİKRO parçayı ayıklamalıdır."
    )
    assert current_context["250plus_yakinlik_referans_guncel_sentinel"] == 1
    assert guard.satellite.MIN_HOTSPOT_AREA_M2 == 250


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print(
            "Spektral sınır güncel-Sentinel bağlam öz testi başarılı; "
            "250 m²/alarm/görev değişmedi."
        )
        return

    if not RAW_FILE.exists():
        raise RuntimeError("micro_site_audit.json bulunamadı.")
    raw_payload = json.loads(RAW_FILE.read_text(encoding="utf-8"))
    result = build_review(raw_payload)
    OUTPUT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Spektral sınır MİKRO güncellendi: "
        f"aday={result['toplam_sinir_aday']}, "
        f"temporal+lokal güçlü={result['temporal_lokal_guclu']}, "
        f"güncel-250+={result['250plus_yakinlik_referans_guncel_sentinel']}, "
        f"eski/uygunsuz-ref-dışlandı={result['250plus_yakinlik_referans_dislanan_eski_veya_250alti']}. "
        "Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
