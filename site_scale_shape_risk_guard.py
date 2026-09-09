"""250 m²+ Sentinel adaylarında şantiye-ölçeği şekil riskini görünür kılar.

Mevcut ``shape_false_positive_audit.py`` uzun-ince/düşük-kompaktlık metriklerini
800 m² üstü-10.000 m² bandı için sayıyor fakat örnek koordinatları yalnız 10.000 m² üstü
geniş-yüzey sınıfında yayımlıyor. Bu diagnostik katman o boşluğu kapatır.

Hiçbir adayı silmez, alarm/rota/saha görevi üretmez ve 250 m² ana üretim eşiğine
veya 150-249 m² MİKRO politikasına dokunmaz. Amaç; tarla/toprak temizliği,
yol/altyapı veya başka lineer-homojen hareket olasılığı taşıyan şantiye-ölçeği
kümeleri koordinatlı olarak kalibrasyona taşımaktır. Şekil tek başına sınıflandırma
kanıtı değildir; güçlü temporal/lokal yapılaşma sinyali varsa aday izlenmeye devam eder.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import satellite
import shape_false_positive_audit as shape_audit
from daily_report import ISTANBUL, REPORT_REGIONS, ensure_daily_schema
from scanner import connect


OUTPUT_FILE = Path(__file__).with_name("site_scale_shape_risk_review.json")
# Ana motor 250-800 m² bandını üst sınır DAHİL küçük-saha olarak işler. Diagnostik
# de aynı sınıf sözleşmesini izlemeli; tam 800 m² zayıf bir küme standart şekil
# bandına sızmamalıdır.
SITE_SCALE_MIN_M2 = satellite.SMALL_HOTSPOT_MAX_M2
SITE_SCALE_MAX_M2 = 10_000
EXAMPLE_LIMIT = 8


def _site_scale_risks(records):
    rows = []
    for raw in records or []:
        if not isinstance(raw, dict):
            continue
        try:
            area = float(raw.get("alan_m2") or 0)
        except (TypeError, ValueError):
            continue
        if not (SITE_SCALE_MIN_M2 < area <= SITE_SCALE_MAX_M2):
            continue
        long_thin = bool(raw.get("uzun_ince"))
        low_compactness = bool(raw.get("dusuk_kompaktlik"))
        if not (long_thin or low_compactness):
            continue

        row = dict(raw)
        if long_thin and low_compactness:
            risk_type = "UZUN_INCE_VE_DUSUK_KOMPAKTLIK"
        elif long_thin:
            risk_type = "UZUN_INCE"
        else:
            risk_type = "DUSUK_KOMPAKTLIK"
        row["sekil_riski"] = risk_type
        row["alarm"] = False
        row["saha_gorevi"] = False
        try:
            latitude = float(row.get("enlem"))
            longitude = float(row.get("boylam"))
            row["harita"] = (
                "https://www.google.com/maps/dir/?api=1&destination="
                f"{latitude:.6f},{longitude:.6f}"
            )
        except (TypeError, ValueError):
            pass
        rows.append(row)

    rows.sort(
        key=lambda item: (
            0 if item.get("sekil_riski") == "UZUN_INCE_VE_DUSUK_KOMPAKTLIK" else 1,
            0 if item.get("dusuk_kompaktlik") else 1,
            float(item.get("kompaktlik") or 1),
            -float(item.get("alan_m2") or 0),
        )
    )
    return rows


def _self_check():
    records = [
        {
            "enlem": 38.3,
            "boylam": 26.6,
            "alan_m2": 1200,
            "uzun_ince": False,
            "dusuk_kompaktlik": True,
            "kompaktlik": 0.12,
        },
        {
            "enlem": 38.3,
            "boylam": 26.61,
            "alan_m2": 900,
            "uzun_ince": True,
            "dusuk_kompaktlik": False,
            "kompaktlik": 0.3,
        },
        {
            "enlem": 38.3,
            "boylam": 26.62,
            "alan_m2": 700,
            "uzun_ince": True,
            "dusuk_kompaktlik": True,
            "kompaktlik": 0.1,
        },
        {
            "enlem": 38.3,
            "boylam": 26.63,
            "alan_m2": 11000,
            "uzun_ince": True,
            "dusuk_kompaktlik": True,
            "kompaktlik": 0.1,
        },
        {
            "enlem": 38.3,
            "boylam": 26.64,
            "alan_m2": 1500,
            "uzun_ince": False,
            "dusuk_kompaktlik": False,
            "kompaktlik": 0.5,
        },
    ]
    risks = _site_scale_risks(records)
    assert len(risks) == 2, risks
    assert risks[0]["sekil_riski"] == "DUSUK_KOMPAKTLIK", risks
    assert all(item["alarm"] is False for item in risks), risks
    assert all(item["saha_gorevi"] is False for item in risks), risks
    assert not _site_scale_risks(
        [{**records[0], "alan_m2": satellite.SMALL_HOTSPOT_MAX_M2}]
    ), "Tam 800 m² ana motor gibi küçük-saha bandında kalmalı"
    assert _site_scale_risks(
        [{**records[0], "alan_m2": satellite.SMALL_HOTSPOT_MAX_M2 + 0.01}]
    ), "800 m² üstü şantiye-ölçeği diagnostik banda girmeli"
    assert _site_scale_risks([{**records[0], "alan_m2": 10000}]), "10.000 m² dahil olmalı"


def _capture_region_records(region_key, pair, final_candidates):
    """Mevcut şekil denetiminin hesapladığı kayıtları tekrar kullanır.

    ``_analyze_region`` içinde ``_flagged_wide`` üç kez çağrılır: ham seçim,
    nihai tam-eşleşen seçim ve yaklaşık eşleşme dahil çözülen nihai seçim.
    Fonksiyonu geçici olarak sarmalayarak aynı raster hesabını değiştirmeden
    bu kayıtları yakalarız. Üretim koduna hiçbir yan etki bırakmamak için global
    fonksiyon ``finally`` içinde mutlaka geri yüklenir.
    """
    captured_calls = []
    original_flagger = shape_audit._flagged_wide

    def capture(records):
        captured_calls.append([dict(item) for item in records or [] if isinstance(item, dict)])
        return original_flagger(records)

    shape_audit._flagged_wide = capture
    try:
        shape_audit._analyze_region(
            region_key,
            pair,
            final_candidates=final_candidates,
        )
    finally:
        shape_audit._flagged_wide = original_flagger

    if len(captured_calls) < 3:
        raise RuntimeError(
            "shape_false_positive_audit çağrı sözleşmesi değişti; "
            f"3 kayıt seti beklenirken {len(captured_calls)} bulundu"
        )
    return {
        "ham_secim": captured_calls[0],
        "nihai_tam_eslesen": captured_calls[1],
        "nihai_cozulen": captured_calls[2],
    }


def build_review():
    _self_check()
    ensure_daily_schema()
    now = datetime.now(ISTANBUL)
    report_date = now.strftime("%Y-%m-%d")
    regions = {}

    with connect() as connection:
        for region_key in REPORT_REGIONS:
            snapshot = shape_audit._today_snapshot(connection, report_date, region_key)
            record = {
                "bolge": satellite.REGIONS[region_key]["label"],
                "son_item": snapshot.get("son_item"),
                "durum": "ok",
            }
            if snapshot.get("hata"):
                record["durum"] = "gunluk_uydu_hatasi"
                record["hata"] = str(snapshot.get("hata"))
                regions[region_key] = record
                continue

            try:
                pair = satellite.sentinel_pair(region_key)
                latest_item = pair[1].get("id")
                record["latest_item"] = latest_item
                if not snapshot.get("son_item") or snapshot.get("son_item") != latest_item:
                    record["durum"] = "gunluk_rapor_latest_ile_eslesmiyor"
                    regions[region_key] = record
                    continue

                captured = _capture_region_records(
                    region_key,
                    pair,
                    snapshot.get("hareket", []),
                )
                raw_risks = _site_scale_risks(captured["ham_secim"])
                final_risks = _site_scale_risks(captured["nihai_tam_eslesen"])
                resolved_risks = _site_scale_risks(captured["nihai_cozulen"])
                record.update(
                    {
                        "ham_sekil_riski": len(raw_risks),
                        "nihai_sekil_riski": len(final_risks),
                        "nihai_cozulen_sekil_riski": len(resolved_risks),
                        "nihai_ornekler": final_risks[:EXAMPLE_LIMIT],
                        "nihai_cozulen_ornekler": resolved_risks[:EXAMPLE_LIMIT],
                    }
                )
            except Exception as exc:
                record["durum"] = "denetim_hatasi"
                record["hata"] = f"{type(exc).__name__}: {exc}"
            regions[region_key] = record

    total_final = sum(
        int(item.get("nihai_sekil_riski") or 0)
        for item in regions.values()
        if isinstance(item, dict)
    )
    payload = {
        "rapor_tarihi": report_date,
        "olusturma": now.strftime("%Y-%m-%d %H:%M %z"),
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": satellite.MIN_HOTSPOT_AREA_M2,
        "mikro_aralik_m2": [150, 249],
        "diagnostik_sekil_risk_bandi_m2": [SITE_SCALE_MIN_M2, SITE_SCALE_MAX_M2],
        "diagnostik_alt_sinir_dahil": False,
        "esikler": {
            "uzun_ince_min_uzun_kisa_orani": shape_audit.LONG_THIN_ASPECT_MIN,
            "dusuk_kompaktlik_max": shape_audit.LOW_COMPACTNESS_MAX,
        },
        "nihai_sekil_riski_toplam": total_final,
        "bolgeler": regions,
        "yorum": (
            "Şekil riski tek başına yol, tarla veya yanlış pozitif kararı değildir. "
            "800 m² üstü-10.000 m² bandındaki uzun-ince/düşük-kompakt kümeler silinmez; "
            "temporal/lokal değişim, saha geri bildirimi ve güvenilir ek kanıtla "
            "kalibre edilmek üzere koordinatlı arka-plan diagnostik olarak tutulur. "
            "250 m² ana eşik ve 150-249 m² MİKRO politikası değişmez."
        ),
    }
    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("site_scale_shape_risk_guard self-check: ok")
        return
    payload = build_review()
    print(
        "site-scale shape risk:",
        payload.get("nihai_sekil_riski_toplam"),
        "diagnostik aday",
    )


if __name__ == "__main__":
    main()
