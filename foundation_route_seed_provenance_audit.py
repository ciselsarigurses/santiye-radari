"""Temel/kepçe operasyon rotasının seed-merkezli S2 kaynağını doğrular.

Bu denetim yalnız diagnostiktir; rota, alarm, saha görevi, 250 m² ana eşik veya
150–249 m² MİKRO politikasını değiştirmez. FOUNDATION_POSITIVE_CONTEXT kaynaklı
operasyon kayıtları generic üretim maskesinden değil seed-merkezli lokal morfoloji
havuzundan gelebilir. Bu nedenle yalnız bu kaynak tipindeki rota koordinatının gerçek
kaynak kaydıyla aynı Sentinel-2 tarih çiftinde ve aynı noktada izlenebilir olduğunu
doğrular. Post-onset SAR süreklilik gibi ayrı provenance yoluna sahip adayları bu
audit seed-merkezli kaynakmış gibi zorlamaz.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


BASE = Path(__file__).resolve().parent
ROUTE_JSON = BASE / "operational_route.json"
SEED_JSON = BASE / "seed_centered_excavation_morphology_review.json"
OUTPUT_JSON = BASE / "foundation_route_seed_provenance_review.json"
MATCH_RADIUS_M = 5.0
MIN_AREA_SIMILARITY = 0.80
MIN_MORPHOLOGY_SCORE = 65.0


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
    lat1, lon1 = float(a[0]), float(a[1])
    lat2, lon2 = float(b[0]), float(b[1])
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _area(item):
    try:
        return float(item.get("alan_m2") or item.get("spektral_etki_alani_m2") or 0)
    except (TypeError, ValueError):
        return 0.0


def _area_similarity(a, b):
    a = max(float(a), 0.0)
    b = max(float(b), 0.0)
    maximum = max(a, b)
    if maximum <= 0:
        return 1.0
    return min(a, b) / maximum


def _foundation_routes(route):
    return [
        row
        for row in (route.get("operasyonel_rota") or [])
        if isinstance(row, dict)
        and row.get("temel_kazi_coklu_kanit_destekli") is True
        and str(row.get("kaynak") or "") == "FOUNDATION_POSITIVE_CONTEXT"
        and _point(row) is not None
    ]


def _label_tokens(label):
    return {
        part.strip().casefold()
        for part in str(label or "").split("·")
        if part.strip()
    }


def _region_key(route_row, seed):
    label = str(route_row.get("bolge") or "")
    regions = seed.get("bolgeler") or {}

    # Önce tam etiketi tercih et; mevcut Çeşme yolu değişmeden kalır.
    for key, region in regions.items():
        if isinstance(region, dict) and str(region.get("bolge") or "") == label:
            return str(key)

    # Operasyonel rota Gülbahçe'yi bilinçli olarak etiketten çıkarabilir. Seed kaydı
    # daha geniş kaynak etiketi taşısa da aynı çekirdek bölgeyi fail-closed biçimde
    # eşleştir: rota tokenlarının tamamı kaynak etikette olmalı ve tek eşleşme olmalı.
    route_tokens = _label_tokens(label)
    if not route_tokens:
        return None
    matches = []
    for key, region in regions.items():
        if not isinstance(region, dict):
            continue
        source_tokens = _label_tokens(region.get("bolge"))
        if route_tokens <= source_tokens:
            matches.append(str(key))
    return matches[0] if len(matches) == 1 else None


def _nearest(route_row, candidates):
    target = _point(route_row)
    nearest = None
    nearest_distance = None
    for candidate in candidates:
        if not isinstance(candidate, dict) or _point(candidate) is None:
            continue
        distance = _distance_m(target, _point(candidate))
        if nearest_distance is None or distance < nearest_distance:
            nearest = candidate
            nearest_distance = distance
    return nearest, nearest_distance


def _audit_row(route_row, seed):
    key = _region_key(route_row, seed)
    base = {
        "gorev_id": route_row.get("gorev_id"),
        "enlem": route_row.get("enlem"),
        "boylam": route_row.get("boylam"),
        "rota_alan_m2": round(_area(route_row)),
        "rota_morfoloji_puani": route_row.get("temel_kazi_morfoloji_puani"),
        "rota_onceki_tarih": route_row.get("onceki_tarih"),
        "rota_son_tarih": route_row.get("son_tarih"),
        "kaynak": route_row.get("kaynak"),
    }
    if key is None:
        return {**base, "durum": "BOLGE_ESLESMEDI"}

    region = (seed.get("bolgeler") or {}).get(key) or {}
    source_previous = str(region.get("onceki_tarih") or "").strip()
    source_latest = str(region.get("son_tarih") or "").strip()
    route_previous = str(route_row.get("onceki_tarih") or "").strip()
    route_latest = str(route_row.get("son_tarih") or "").strip()
    pair_ok = bool(
        source_previous
        and source_latest
        and route_previous == source_previous
        and route_latest == source_latest
    )

    nearest, distance = _nearest(route_row, region.get("adaylar") or [])
    if nearest is None or distance is None:
        return {
            **base,
            "bolge_anahtari": key,
            "kaynak_onceki_tarih": source_previous or None,
            "kaynak_son_tarih": source_latest or None,
            "tarih_cifti_eslesiyor": pair_ok,
            "durum": "KAYNAK_ADAY_YOK",
        }

    source_area = _area(nearest)
    area_similarity = _area_similarity(_area(route_row), source_area)
    try:
        source_score = float(nearest.get("seed_merkezli_morfoloji_puani") or 0)
    except (TypeError, ValueError):
        source_score = 0.0
    source_level = str(nearest.get("seed_merkezli_morfoloji_seviyesi") or "").upper()
    coordinate_ok = distance <= MATCH_RADIUS_M
    morphology_ok = source_level == "YUKSEK" and source_score >= MIN_MORPHOLOGY_SCORE
    area_ok = area_similarity >= MIN_AREA_SIMILARITY
    verified = bool(pair_ok and coordinate_ok and morphology_ok and area_ok)

    return {
        **base,
        "bolge_anahtari": key,
        "kaynak_onceki_tarih": source_previous or None,
        "kaynak_son_tarih": source_latest or None,
        "tarih_cifti_eslesiyor": pair_ok,
        "kaynak_eslesme_mesafe_m": round(distance, 1),
        "kaynak_enlem": nearest.get("enlem"),
        "kaynak_boylam": nearest.get("boylam"),
        "kaynak_alan_m2": round(source_area),
        "alan_benzerligi": round(area_similarity, 3),
        "kaynak_morfoloji_puani": source_score,
        "kaynak_morfoloji_seviyesi": source_level or None,
        "kaynak_seed_sinifi": nearest.get("seed_sinifi"),
        "kaynak_seed_piksel": nearest.get("seed_piksel"),
        "kaynak_pozitif_kanitlar": nearest.get("pozitif_kanitlar") or [],
        "kaynak_negatif_kanitlar": nearest.get("negatif_kanitlar") or [],
        "koordinat_eslesiyor": coordinate_ok,
        "alan_eslesiyor": area_ok,
        "morfoloji_eslesiyor": morphology_ok,
        "durum": "KAYNAK_DOGRULANDI" if verified else "KAYNAK_ESLESMEDI",
        "otomatik_rota_degistirme": False,
    }


def audit(route=None, seed=None):
    route = route if isinstance(route, dict) else _load(ROUTE_JSON)
    seed = seed if isinstance(seed, dict) else _load(SEED_JSON)
    rows = [_audit_row(row, seed) for row in _foundation_routes(route)]
    verified = sum(1 for row in rows if row.get("durum") == "KAYNAK_DOGRULANDI")
    return {
        "surum": 2,
        "amac": "FOUNDATION_POSITIVE_CONTEXT operasyon kaydını gerçek seed-merkezli S2 morfoloji kaynağına geri izlemek",
        "gercek_derinlik_olcumu": False,
        "alarm": False,
        "saha_gorevi": False,
        "uretim_filtresi": False,
        "eslesme_yaricapi_m": MATCH_RADIUS_M,
        "minimum_alan_benzerligi": MIN_AREA_SIMILARITY,
        "minimum_morfoloji_puani": MIN_MORPHOLOGY_SCORE,
        "rota_hedefi": len(rows),
        "kaynak_dogrulanan": verified,
        "kaynak_dogrulanamayan": len(rows) - verified,
        "hedefler": rows,
        "not": (
            "Yalnız FOUNDATION_POSITIVE_CONTEXT kaynaklı rota kayıtları bu seed-merkezli provenance yolunda denetlenir. "
            "Generic üretim maskesiyle eşleşmeme tek başına bu seed-merkezli yolu veto etmez. "
            "Operasyonel etikette Gülbahçe'nin çıkarılması halinde çekirdek Uzunkuyu/Germiyan/Ildır tokenları tek bir "
            "seed bölgesine fail-closed eşleşebilir. Saha teyidinin yerini tutmaz."
        ),
    }


def _self_check():
    route = {
        "operasyonel_rota": [
            {
                "gorev_id": "FKTEST",
                "enlem": 38.307849,
                "boylam": 26.333503,
                "alan_m2": 400,
                "bolge": "Çeşme merkez · Alaçatı · Ilıca",
                "onceki_tarih": "15.09.2026",
                "son_tarih": "18.09.2026",
                "temel_kazi_coklu_kanit_destekli": True,
                "uydu_diagnostik_adayi": True,
                "kaynak": "FOUNDATION_POSITIVE_CONTEXT",
                "temel_kazi_morfoloji_puani": 95,
            },
            {
                "gorev_id": "POSTTEST",
                "enlem": 38.278725,
                "boylam": 26.303853,
                "alan_m2": 500,
                "bolge": "Çeşme merkez · Alaçatı · Ilıca",
                "onceki_tarih": "13.09.2026",
                "son_tarih": "18.09.2026",
                "temel_kazi_coklu_kanit_destekli": True,
                "uydu_diagnostik_adayi": True,
                "kaynak": "POST_ONSET_SAR_PERSISTENCE",
                "temel_kazi_morfoloji_puani": 100,
            },
        ]
    }
    seed = {
        "bolgeler": {
            "cesme": {
                "bolge": "Çeşme merkez · Alaçatı · Ilıca",
                "onceki_tarih": "15.09.2026",
                "son_tarih": "18.09.2026",
                "adaylar": [
                    {
                        "enlem": 38.307849,
                        "boylam": 26.333503,
                        "spektral_etki_alani_m2": 400,
                        "seed_merkezli_morfoloji_puani": 95,
                        "seed_merkezli_morfoloji_seviyesi": "YUKSEK",
                        "seed_sinifi": "PARLAK_LOKAL",
                        "seed_piksel": 4,
                    }
                ],
            },
            "uzunkuyu": {
                "bolge": "Uzunkuyu · Germiyan · Ildır · Gülbahçe",
                "onceki_tarih": "15.09.2026",
                "son_tarih": "18.09.2026",
                "adaylar": [
                    {
                        "enlem": 38.262806,
                        "boylam": 26.477305,
                        "spektral_etki_alani_m2": 300,
                        "seed_merkezli_morfoloji_puani": 90,
                        "seed_merkezli_morfoloji_seviyesi": "YUKSEK",
                        "seed_sinifi": "DUSUK_KONTRAST_KAZI_PROXY",
                        "seed_piksel": 3,
                    }
                ],
            },
        }
    }
    payload = audit(route, seed)
    assert payload["rota_hedefi"] == 1, payload
    assert payload["kaynak_dogrulanan"] == 1, payload
    assert payload["hedefler"][0]["durum"] == "KAYNAK_DOGRULANDI", payload

    bad_pair = json.loads(json.dumps(route))
    bad_pair["operasyonel_rota"][0]["onceki_tarih"] = "13.09.2026"
    payload = audit(bad_pair, seed)
    assert payload["kaynak_dogrulanan"] == 0, payload
    assert payload["hedefler"][0]["tarih_cifti_eslesiyor"] is False, payload

    far = json.loads(json.dumps(seed))
    far["bolgeler"]["cesme"]["adaylar"][0]["enlem"] = 38.3085
    payload = audit(route, far)
    assert payload["kaynak_dogrulanan"] == 0, payload

    uzunkuyu_route = {
        "operasyonel_rota": [
            {
                "gorev_id": "FKUZUNKUYU",
                "enlem": 38.262806,
                "boylam": 26.477305,
                "alan_m2": 300,
                "bolge": "Uzunkuyu · Germiyan · Ildır",
                "onceki_tarih": "15.09.2026",
                "son_tarih": "18.09.2026",
                "temel_kazi_coklu_kanit_destekli": True,
                "uydu_diagnostik_adayi": True,
                "kaynak": "FOUNDATION_POSITIVE_CONTEXT",
                "temel_kazi_morfoloji_puani": 90,
            }
        ]
    }
    payload = audit(uzunkuyu_route, seed)
    assert payload["rota_hedefi"] == 1, payload
    assert payload["kaynak_dogrulanan"] == 1, payload
    assert payload["hedefler"][0]["bolge_anahtari"] == "uzunkuyu", payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("foundation route seed provenance audit self-check: ok")
        return
    payload = audit()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
