"""Gülbahçe mikro-şantiye kalite körlüğünü kapasite açısından yorumlar.

Bu koruma yeni Sentinel sorgusu yapmaz. ``micro_site_audit.json`` içindeki iki-sahne
temporal kalite körlüğünü, ``gulbahce_latest_state_blind_review.json`` içindeki en
yeni sahnenin gerçek kalite körlüğünden ayrı tutar. Operasyonel mikro-körlük
kapasitesi yalnız aynı güncel Sentinel sahnesi doğrulanmışsa hesaplanır.

Alarm veya saha görevi üretmez ve ana 250 m² eşiğini değiştirmez.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


INPUT_FILE = Path(__file__).with_name("micro_site_audit.json")
LATEST_STATE_FILE = Path(__file__).with_name("gulbahce_latest_state_blind_review.json")
OUTPUT_FILE = Path(__file__).with_name("gulbahce_micro_blind_capacity.json")
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
MAX_BLIND_EXAMPLES = 8


def _as_nonnegative_int(value):
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _blind_examples(raw_rows, limit=MAX_BLIND_EXAMPLES, reason_default="KALITE_KORLUGU"):
    """150 m²+ kör cepleri koordinatlı diagnostik olarak korur."""
    rows = []
    for raw in raw_rows or []:
        if not isinstance(raw, dict):
            continue
        try:
            latitude = float(raw["enlem"])
            longitude = float(raw["boylam"])
            area = float(raw["alan_m2"])
        except (KeyError, TypeError, ValueError):
            continue
        if area < MICRO_RANGE_M2[0]:
            continue

        rows.append(
            {
                "enlem": round(latitude, 6),
                "boylam": round(longitude, 6),
                "alan_m2": int(round(area)),
                "neden": str(raw.get("neden") or reason_default),
                "boyut_sinifi": (
                    "MIKRO_KOR"
                    if area < MAIN_THRESHOLD_M2
                    else "ANA_KOR_MIKRO_GIZLEYEBILIR"
                ),
                "mikro_gizleyebilir": True,
                "alarm": False,
                "saha_gorevi": False,
                "harita": (
                    "https://www.google.com/maps/dir/?api=1&destination="
                    f"{latitude:.6f},{longitude:.6f}"
                ),
            }
        )

    rows.sort(
        key=lambda item: (
            0 if item["alan_m2"] < MAIN_THRESHOLD_M2 else 1,
            item["alan_m2"],
            item["enlem"],
            item["boylam"],
        )
    )
    return rows[: max(int(limit), 0)]


def _latest_state_matches(uzunkuyu, latest_state):
    """Güncel-körlük review'ünün aynı Sentinel sahnesine ait olduğunu doğrular."""
    if not isinstance(latest_state, dict) or latest_state.get("durum") != "ok":
        return False
    if latest_state.get("ayni_sentinel_sahnesi") is not True:
        return False

    source_item = str(uzunkuyu.get("son_item") or "")
    review_item = str(latest_state.get("kaynak_son_item") or "")
    if source_item and review_item:
        return source_item == review_item

    source_date = str(uzunkuyu.get("son_tarih") or "")
    review_date = str(latest_state.get("kaynak_son_tarih") or "")
    return bool(source_date and review_date and source_date == review_date)


def build_guard(payload, latest_state=None):
    regions = payload.get("bolgeler") or {}
    uzunkuyu = regions.get("uzunkuyu") or {}
    observability = uzunkuyu.get("gulbahce_gozlenebilirlik") or {}

    base = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": payload.get("ana_uretim_esigi_m2", MAIN_THRESHOLD_M2),
        "mikro_aralik_m2": payload.get("mikro_aralik_m2", MICRO_RANGE_M2),
        "amac": (
            "Gülbahçe'de iki-sahne temporal kalite körlüğünü en yeni sahnenin gerçek "
            "körlüğünden ayırmak; mikro körlük kapasitesini yalnız güncel-sahne "
            "kanıtından hesaplamak."
        ),
    }

    if not observability:
        return {
            **base,
            "durum": "veri_yok",
            "guncel_sahne_dogrulandi": False,
            "mikro_kor_kume_tam_150_249": 0,
            "ana_kor_kume_250plus": 0,
            "mikro_gizleyebilen_kor_kume": 0,
            "mikro_korluk_kapasitesi_var": False,
            "kor_kume_ornekleri": [],
            "temporal_cift_sahne_kor_kume_150_249": 0,
            "temporal_cift_sahne_kor_kume_250plus": 0,
            "temporal_cift_sahne_mikro_gizleyebilen_kor_kume": 0,
            "temporal_cift_sahne_kor_kume_ornekleri": [],
            "yorum": (
                "Gülbahçe gözlenebilirlik verisi yok; negatif operasyonel sonuç "
                "üretilmedi. Bir sonraki mikro audit beklenir."
            ),
        }

    temporal_micro = _as_nonnegative_int(observability.get("mikro_kor_kume_150_249"))
    temporal_main = _as_nonnegative_int(observability.get("ana_kor_kume_250plus"))
    temporal_examples = _blind_examples(observability.get("kor_kume_ornekleri"))

    latest_ok = _latest_state_matches(uzunkuyu, latest_state)
    current_micro = None
    current_main = None
    current_capable = None
    current_examples = []

    if latest_ok:
        current_micro = _as_nonnegative_int(
            latest_state.get("guncel_sahne_mikro_kor_150_249")
        )
        current_main = _as_nonnegative_int(
            latest_state.get("guncel_sahne_kor_250_6500")
        )
        current_capable = current_micro + current_main
        current_examples = _blind_examples(
            (latest_state.get("guncel_sahne_mikro_kor_kumeleri") or [])
            + (latest_state.get("guncel_sahne_kor_kumeleri") or []),
            reason_default="GUNCEL_SAHNE_KALITE_KORLUGU",
        )

    return {
        **base,
        "durum": "ok" if latest_ok else "guncel_sahne_verisi_dogrulanamadi",
        "kaynak_son_tarih": uzunkuyu.get("son_tarih"),
        "kaynak_son_item": uzunkuyu.get("son_item"),
        "gulbahce_operasyon_yaricapi_m": (
            (observability.get("referans") or {}).get("operasyon_yaricapi_m")
        ),
        "mikro_ham_aday_2km": _as_nonnegative_int(
            observability.get("mikro_ham_aday_2km")
        ),
        "guncel_sahne_dogrulandi": latest_ok,
        "guncel_sahne_review_tarih": (
            latest_state.get("kaynak_son_tarih")
            if isinstance(latest_state, dict)
            else None
        ),
        "guncel_sahne_review_item": (
            latest_state.get("kaynak_son_item")
            if isinstance(latest_state, dict)
            else None
        ),
        "guncel_sahne_mikro_kor_150_249": current_micro,
        "guncel_sahne_kor_250_6500": current_main,
        "guncel_sahne_buyuk_kor_6500ustu_arka_plan": (
            _as_nonnegative_int(latest_state.get("guncel_sahne_kor_6500ustu"))
            if latest_ok
            else None
        ),
        # Eski genel alan adları artık yalnız doğrulanmış EN YENİ sahne anlamına gelir.
        "mikro_kor_kume_tam_150_249": current_micro,
        "ana_kor_kume_250plus": current_main,
        "mikro_gizleyebilen_kor_kume": current_capable,
        "mikro_korluk_kapasitesi_var": (
            current_capable > 0 if current_capable is not None else None
        ),
        "kor_kume_ornekleri": current_examples,
        # İki-sahne gerektiren MİKRO temporal görünürlük ayrı diagnostik olarak korunur.
        "kalite_kor_kara_yuzde_temporal_cift_sahne": observability.get(
            "kalite_kor_kara_yuzde"
        ),
        "temporal_cift_sahne_kor_kume_150_249": temporal_micro,
        "temporal_cift_sahne_kor_kume_250plus": temporal_main,
        "temporal_cift_sahne_mikro_gizleyebilen_kor_kume": (
            temporal_micro + temporal_main
        ),
        "temporal_cift_sahne_kor_kume_ornekleri": temporal_examples,
        "yorum": (
            "İki-sahne temporal kalite körlüğü, en yeni sahnenin körlüğü değildir. "
            "Mikro körlük kapasitesi yalnız micro audit ile aynı en yeni Sentinel "
            "sahnesine ait güncel-sahne review'ü doğrulanırsa hesaplanır. Temporal "
            "çift-sahne cepleri silinmez; arka-plan diagnostik olarak korunur. "
            "6500 m² üstü güncel körlük geniş-yüzey arka planında ayrıca izlenir. "
            "Bu katman alarm veya saha görevi üretmez."
        ),
    }


def _self_check():
    assert MAIN_THRESHOLD_M2 == 250
    sample = {
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "bolgeler": {
            "uzunkuyu": {
                "son_tarih": "05.09.2026",
                "son_item": "S2_TEST",
                "gulbahce_gozlenebilirlik": {
                    "referans": {"operasyon_yaricapi_m": 2000},
                    "mikro_kor_kume_150_249": 1,
                    "ana_kor_kume_250plus": 1,
                    "mikro_ham_aday_2km": 0,
                    "kalite_kor_kara_yuzde": 0.035,
                    "kor_kume_ornekleri": [
                        {"enlem": 38.33, "boylam": 26.64, "alan_m2": 400, "neden": "BULUT"},
                        {"enlem": 38.34, "boylam": 26.65, "alan_m2": 200, "neden": "GOLGE"},
                    ],
                },
            }
        },
    }

    latest_clear = {
        "durum": "ok",
        "ayni_sentinel_sahnesi": True,
        "kaynak_son_tarih": "05.09.2026",
        "kaynak_son_item": "S2_TEST",
        "guncel_sahne_mikro_kor_150_249": 0,
        "guncel_sahne_kor_250_6500": 0,
        "guncel_sahne_kor_6500ustu": 0,
        "guncel_sahne_mikro_kor_kumeleri": [],
        "guncel_sahne_kor_kumeleri": [],
    }
    clear_result = build_guard(sample, latest_clear)
    assert clear_result["durum"] == "ok"
    assert clear_result["guncel_sahne_dogrulandi"] is True
    assert clear_result["mikro_gizleyebilen_kor_kume"] == 0
    assert clear_result["mikro_korluk_kapasitesi_var"] is False
    assert clear_result["kor_kume_ornekleri"] == []
    assert clear_result["temporal_cift_sahne_mikro_gizleyebilen_kor_kume"] == 2
    assert len(clear_result["temporal_cift_sahne_kor_kume_ornekleri"]) == 2

    latest_blind = {
        **latest_clear,
        "guncel_sahne_mikro_kor_150_249": 1,
        "guncel_sahne_kor_250_6500": 1,
        "guncel_sahne_mikro_kor_kumeleri": [
            {"enlem": 38.35, "boylam": 26.66, "alan_m2": 200, "neden": "GUNCEL_BULUT"}
        ],
        "guncel_sahne_kor_kumeleri": [
            {"enlem": 38.36, "boylam": 26.67, "alan_m2": 400, "neden": "GUNCEL_GOLGE"}
        ],
    }
    blind_result = build_guard(sample, latest_blind)
    assert blind_result["mikro_gizleyebilen_kor_kume"] == 2
    assert blind_result["mikro_korluk_kapasitesi_var"] is True
    assert len(blind_result["kor_kume_ornekleri"]) == 2
    assert blind_result["kor_kume_ornekleri"][0]["boyut_sinifi"] == "MIKRO_KOR"
    assert all(
        not item["alarm"] and not item["saha_gorevi"]
        for item in blind_result["kor_kume_ornekleri"]
    )

    stale = {**latest_clear, "kaynak_son_item": "S2_OLD"}
    stale_result = build_guard(sample, stale)
    assert stale_result["durum"] == "guncel_sahne_verisi_dogrulanamadi"
    assert stale_result["guncel_sahne_dogrulandi"] is False
    assert stale_result["mikro_korluk_kapasitesi_var"] is None
    assert stale_result["temporal_cift_sahne_mikro_gizleyebilen_kor_kume"] == 2

    missing = build_guard({"ana_uretim_esigi_m2": 250, "bolgeler": {}}, latest_clear)
    assert missing["durum"] == "veri_yok"
    assert missing["mikro_korluk_kapasitesi_var"] is False
    assert missing["kor_kume_ornekleri"] == []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print(
            "Gülbahçe mikro körlük kapasitesi öz testi başarılı; "
            "güncel-sahne ve temporal çift-sahne körlüğü ayrıldı, 250 m² eşik değişmedi."
        )
        return

    if not INPUT_FILE.exists():
        raise RuntimeError("micro_site_audit.json bulunamadı.")
    payload = json.loads(INPUT_FILE.read_text(encoding="utf-8"))
    latest_state = (
        json.loads(LATEST_STATE_FILE.read_text(encoding="utf-8"))
        if LATEST_STATE_FILE.exists()
        else None
    )
    result = build_guard(payload, latest_state)
    OUTPUT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Gülbahçe mikro körlük kapasitesi: "
        f"durum={result['durum']}, "
        f"güncel-doğrulandı={result.get('guncel_sahne_dogrulandi')}, "
        f"güncel-mikro={result.get('guncel_sahne_mikro_kor_150_249')}, "
        f"güncel-250-6500={result.get('guncel_sahne_kor_250_6500')}, "
        f"temporal-çift-sahne={result.get('temporal_cift_sahne_mikro_gizleyebilen_kor_kume')}. "
        "Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
