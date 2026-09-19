"""Nihai operasyon rotasını Çeşme–Uzunkuyu hattı içinde tutar.

Bu son-kapı guard'ı yeni alarm veya saha görevi üretmez; Sentinel eşiklerini,
150–249 m² MİKRO politikasını ya da algılama/kalibrasyon kayıtlarını değiştirmez.
Yalnız ``operational_route.json`` içindeki kullanıcıya operasyonel olarak sunulan
rotayı süzer. Dışarı alınan kayıtlar silinmez; diagnostik arka planda korunur.

Kullanıcı odağı Çeşme yarımadasından Uzunkuyu'ya kadar olan hattır. Uzunkuyu için
saha kalibrasyonunda verilen 26.562138 boylamının doğusunda ölçüm/etiket belirsizliği
payı bırakmak amacıyla 26.600000 konservatif operasyon sınırı kullanılır. Bu sınır
bir mahalle/ada-parsel iddiası değildir; yalnız saha ekibinin günlük rota sınırıdır.
Açık Gülbahçe, ``rota_uygun=False`` ve mevcut müşteri kayıtları da aynı son kapıda
operasyon listesinden ayrılır.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parent
ROUTE_FILE = BASE / "operational_route.json"
UZUNKUYU_REFERENCE_LON = 26.562138
OPERATIONAL_EAST_LIMIT_LON = 26.600000
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]


def _normalize(value: Any) -> str:
    return str(value or "").strip().casefold()


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _block_reason(item: dict[str, Any]) -> str | None:
    if item.get("rota_uygun") is False:
        return "Kaynak raporda rota_uygun=false; son operasyon rotasına alınmadı."
    if item.get("mevcut_musteri") is True:
        return "Doğrulanmış mevcut müşteri sahası; yeni satış operasyon rotasına alınmadı."

    for key in ("mahalle", "mahalle_yaklasik", "mevki"):
        if "gülbahçe" in _normalize(item.get(key)):
            return "Açık Gülbahçe etiketi operasyonel öncelik dışında; diagnostik izleme sürer."

    longitude = _number(item.get("boylam"))
    if longitude is not None and longitude > OPERATIONAL_EAST_LIMIT_LON:
        return (
            "Çeşme–Uzunkuyu günlük operasyon koridorunun konservatif doğu sınırının "
            "ötesinde; mahalle tahmini yapılmadan diagnostik arka planda tutuldu."
        )
    return None


def apply_corridor_guard(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    raw_route = payload.get("operasyonel_rota") or []
    if not isinstance(raw_route, list):
        raw_route = []

    kept: list[dict[str, Any]] = []
    background: list[dict[str, Any]] = []
    for raw in raw_route:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        reason = _block_reason(item)
        if reason is None:
            kept.append(item)
            continue

        background_item = dict(item)
        background_item["operasyon_odak_disinda"] = True
        background_item["operasyon_koridor_notu"] = reason
        background.append(background_item)

    for index, item in enumerate(kept, start=1):
        item["gunluk_sira"] = index

    result["operasyonel_rota"] = kept
    result["operasyon_odak_arka_plan"] = background
    result["operasyon_odak_arka_plan_sayi"] = len(background)
    result["operasyon_odak_sonrasi_rota_gorevleri"] = [
        str(item.get("gorev_id") or "") for item in kept
    ]
    result["operasyon_koridoru"] = {
        "tanim": "Çeşme yarımadası–Uzunkuyu günlük saha hattı",
        "uzunkuyu_referans_boylam": UZUNKUYU_REFERENCE_LON,
        "konservatif_dogu_siniri_boylam": OPERATIONAL_EAST_LIMIT_LON,
        "mahalle_tahmini": False,
    }
    result["operasyon_koridor_korumasi"] = True
    result["operasyon_koridor_notu"] = (
        "Son kapı yalnız günlük rota sunumunu sınırlar; adayları/alarmları silmez ve "
        "yeni görev üretmez. Açık Gülbahçe, mevcut müşteri, rota_uygun=false veya "
        "26.600000 boylamının doğusundaki belirsiz adaylar diagnostik arka planda kalır. "
        "250 m² ana eşik ve 150–249 m² MİKRO politikası değişmez."
    )
    # Guard davranışının eşik politikasını istemeden değiştirmediğini çıktıda görünür tut.
    if "ana_sentinel_esigi_m2" not in result:
        result["ana_sentinel_esigi_m2"] = MAIN_THRESHOLD_M2
    if "mikro_aralik_m2" not in result:
        result["mikro_aralik_m2"] = MICRO_RANGE_M2
    return result


def _self_check() -> None:
    base = {
        "oncelik": "ERKEN",
        "saha_durumu": "KONTROLE_GIT",
        "rota_uygun": True,
        "alan_m2": 400,
    }
    payload = {
        "alarm": False,
        "saha_gorevi": False,
        "ana_sentinel_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "operasyonel_rota": [
            {**base, "gorev_id": "ILICA", "mahalle": "Ilıca", "enlem": 38.31, "boylam": 26.38},
            {**base, "gorev_id": "UZUNKUYU", "mahalle": "Uzunkuyu", "enlem": 38.32, "boylam": 26.562138},
            {**base, "gorev_id": "EAST", "mahalle": "Mevki doğrulanmadı", "enlem": 38.36845, "boylam": 26.64388},
            {**base, "gorev_id": "GUL", "mahalle": "Gülbahçe", "enlem": 38.33, "boylam": 26.59},
            {**base, "gorev_id": "CUSTOMER", "mahalle": "Ovacık", "enlem": 38.30, "boylam": 26.35, "mevcut_musteri": True},
            {**base, "gorev_id": "BLOCKED", "mahalle": "Musalla", "enlem": 38.30, "boylam": 26.31, "rota_uygun": False},
        ],
    }
    result = apply_corridor_guard(payload)
    ids = [row["gorev_id"] for row in result["operasyonel_rota"]]
    assert ids == ["ILICA", "UZUNKUYU"], ids
    assert [row["gunluk_sira"] for row in result["operasyonel_rota"]] == [1, 2]
    background_ids = {row["gorev_id"] for row in result["operasyon_odak_arka_plan"]}
    assert background_ids == {"EAST", "GUL", "CUSTOMER", "BLOCKED"}
    assert result["ana_sentinel_esigi_m2"] == 250
    assert result["mikro_aralik_m2"] == [150, 249]
    assert result["alarm"] is False
    assert result["saha_gorevi"] is False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.self_check:
        print("operational corridor guard self-check: OK")
        return

    if not ROUTE_FILE.exists():
        raise RuntimeError("operational_route.json bulunamadı")
    payload = json.loads(ROUTE_FILE.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("operational_route.json nesne olmalıdır")
    result = apply_corridor_guard(payload)
    ROUTE_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "operational corridor guard: "
        f"rota={len(result['operasyonel_rota'])} · "
        f"arka_plan={result['operasyon_odak_arka_plan_sayi']}"
    )


if __name__ == "__main__":
    main()
