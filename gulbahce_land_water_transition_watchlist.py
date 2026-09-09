"""Gülbahçe kara->SCL su belirsizliklerini Sentinel sahneleri arasında hafızada tutar.

Bu katman yalnız diagnostiktir. Alarm veya saha görevi üretmez, 250 m² ana üretim
eşiğini değiştirmez ve 150-249 m² MİKRO adaylarını otomatik yükseltmez.

Amaç, tarihsel olarak kara olduğu doğrulanmış kompakt bir alanın güncel Sentinel
SCL sahnesinde su olarak etiketlenmesi halinde bu çelişkinin sonraki sahnede sessizce
kaybolmasını önlemektir. Aynı belirsizlik farklı Sentinel sahnelerinde tekrar ederse
ayrı bir tekrar kanıtı olarak tutulur; sonraki sahne kalite-kör ise iz silinmez; açık
sahnede SCL-su çelişkisi tekrar görülmezse geçmiş kayıt arka planda çözülmüş olarak
saklanır. Hiçbir durum inşaat/kazı kabulü değildir.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from copy import deepcopy
from pathlib import Path


AUDIT_JSON = Path(__file__).with_name("gulbahce_land_water_transition_audit.json")
BLIND_JSON = Path(__file__).with_name("gulbahce_latest_state_blind_review.json")
OUTPUT_JSON = Path(__file__).with_name("gulbahce_land_water_transition_watchlist.json")

MAIN_THRESHOLD_M2 = 250
MICRO_RANGE_M2 = [150, 249]
TRACK_MATCH_RADIUS_M = 75.0
BLIND_MATCH_RADIUS_M = 125.0


def _distance_m(lat1, lon1, lat2, lon2):
    radius = 6371000.0
    p1 = math.radians(float(lat1))
    p2 = math.radians(float(lat2))
    dp = math.radians(float(lat2) - float(lat1))
    dl = math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return radius * 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(1.0 - a, 0.0)))


def _track_id(lat, lon):
    key = f"{float(lat):.5f},{float(lon):.5f}".encode("utf-8")
    return "GWS" + hashlib.sha1(key).hexdigest()[:9].upper()


def _current_candidates(audit):
    rows = []
    for row in audit.get("adaylar") or []:
        if row.get("sinif") != "IC_KARA_SCL_SU_BELIRSIZLIGI":
            continue
        if not row.get("yeniden_goruntule"):
            continue
        try:
            area = float(row.get("alan_m2") or 0)
            lat = float(row["enlem"])
            lon = float(row["boylam"])
        except (KeyError, TypeError, ValueError):
            continue
        if area < MICRO_RANGE_M2[0]:
            continue
        item = deepcopy(row)
        item["enlem"] = lat
        item["boylam"] = lon
        item["alan_m2"] = int(round(area))
        rows.append(item)
    return rows


def _blind_clusters(blind_review):
    rows = []
    for row in blind_review.get("guncel_sahne_kor_kumeleri") or []:
        try:
            rows.append(
                {
                    "enlem": float(row["enlem"]),
                    "boylam": float(row["boylam"]),
                    "alan_m2": int(round(float(row.get("alan_m2") or 0))),
                    "neden": row.get("neden"),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def _nearest_unmatched(candidate, tracks, unmatched_ids):
    best_id = None
    best_distance = None
    for track in tracks:
        track_id = track.get("iz_id")
        if track_id not in unmatched_ids:
            continue
        try:
            distance = _distance_m(
                candidate["enlem"],
                candidate["boylam"],
                track["enlem"],
                track["boylam"],
            )
        except (KeyError, TypeError, ValueError):
            continue
        if distance > TRACK_MATCH_RADIUS_M:
            continue
        if best_distance is None or distance < best_distance:
            best_id = track_id
            best_distance = distance
    return best_id, best_distance


def _nearest_blind(track, blind_clusters):
    best = None
    best_distance = None
    for row in blind_clusters:
        distance = _distance_m(
            track["enlem"],
            track["boylam"],
            row["enlem"],
            row["boylam"],
        )
        if distance > BLIND_MATCH_RADIUS_M:
            continue
        if best_distance is None or distance < best_distance:
            best = row
            best_distance = distance
    return best, best_distance


def _new_track(candidate, source_date, source_item):
    history_dates = [source_date] if source_date else []
    history_items = [source_item] if source_item else []
    return {
        "iz_id": _track_id(candidate["enlem"], candidate["boylam"]),
        "alarm": False,
        "saha_gorevi": False,
        "15_eylul_otomatik_terfi": False,
        "durum": "GUNCEL_SCL_SU_BELIRSIZLIGI",
        "guncel_scl_su_belirsizligi": True,
        "tekrarli_scl_su_belirsizligi": False,
        "farkli_sentinel_sahnesi_gorulme_sayisi": 1 if (source_date or source_item) else 0,
        "sentinel_sahne_tarih_gecmisi": history_dates,
        "sentinel_item_gecmisi": history_items,
        "ilk_gorulme_tarihi": source_date or None,
        "son_gorulme_tarihi": source_date or None,
        "son_sentinel_item": source_item or None,
        "enlem": candidate["enlem"],
        "boylam": candidate["boylam"],
        "ilk_enlem": candidate["enlem"],
        "ilk_boylam": candidate["boylam"],
        "ilk_konumdan_sapma_m": 0.0,
        "alan_m2": candidate["alan_m2"],
        "doluluk_orani": candidate.get("doluluk_orani"),
        "tarihsel_suya_yakin_piksel_orani": candidate.get("tarihsel_suya_yakin_piksel_orani"),
        "son_esleme_mesafe_m": 0.0,
        "kalite_kor_eslesmesi": False,
        "kalite_kor_mesafe_m": None,
        "not": (
            "Tarihsel kara ile güncel SCL=su çelişkisi diagnostik hafızada tutulur. "
            "Bu kayıt inşaat/kazı kanıtı değildir ve alarm/saha görevi üretmez."
        ),
    }


def build_watchlist(audit, blind_review, previous=None):
    previous = previous or {}
    source_date = audit.get("kaynak_son_tarih") or None
    source_item = audit.get("kaynak_son_item") or None
    previous_date = previous.get("son_degerlendirilen_sentinel_tarihi") or None
    previous_item = previous.get("son_degerlendirilen_sentinel_item") or None
    same_scene = bool(
        (source_item and previous_item and source_item == previous_item)
        or (not source_item and source_date and previous_date and source_date == previous_date)
    )

    current = _current_candidates(audit)
    blind_clusters = _blind_clusters(blind_review)
    tracks = [deepcopy(row) for row in (previous.get("izler") or []) if isinstance(row, dict)]

    # Aynı Sentinel sahnesinde tekrar çalışmak sahne sayısını artırmamalı. Yine de
    # ilk kez watchlist kuruluyorsa mevcut adaylardan iz üretmek gerekir.
    if same_scene and tracks:
        result = deepcopy(previous)
        result["alarm"] = False
        result["saha_gorevi"] = False
        result["ana_uretim_esigi_m2"] = MAIN_THRESHOLD_M2
        result["mikro_aralik_m2"] = MICRO_RANGE_M2
        result["ayni_sahne_idempotent"] = True
        return result

    unmatched_ids = {row.get("iz_id") for row in tracks if row.get("iz_id")}
    matched_ids = set()

    for candidate in current:
        matched_id, matched_distance = _nearest_unmatched(candidate, tracks, unmatched_ids)
        if matched_id is None:
            track = _new_track(candidate, source_date, source_item)
            # Aynı koordinattan daha önce çözülmüş bir iz olsa bile 75 m eşleme bunu
            # yakalardı; burada yeni iz gerçekten yeni mekânsal belirsizliktir.
            tracks.append(track)
            continue

        track = next(row for row in tracks if row.get("iz_id") == matched_id)
        unmatched_ids.discard(matched_id)
        matched_ids.add(matched_id)

        dates = list(track.get("sentinel_sahne_tarih_gecmisi") or [])
        items = list(track.get("sentinel_item_gecmisi") or [])
        is_new_observation = False
        if source_item:
            if source_item not in items:
                items.append(source_item)
                is_new_observation = True
        elif source_date and source_date not in dates:
            is_new_observation = True
        if source_date and source_date not in dates:
            dates.append(source_date)

        seen_count = int(track.get("farkli_sentinel_sahnesi_gorulme_sayisi") or 0)
        if is_new_observation:
            seen_count += 1

        track.update(
            {
                "alarm": False,
                "saha_gorevi": False,
                "15_eylul_otomatik_terfi": False,
                "durum": (
                    "TEKRARLI_SCL_SU_BELIRSIZLIGI"
                    if seen_count >= 2
                    else "GUNCEL_SCL_SU_BELIRSIZLIGI"
                ),
                "guncel_scl_su_belirsizligi": True,
                "tekrarli_scl_su_belirsizligi": seen_count >= 2,
                "farkli_sentinel_sahnesi_gorulme_sayisi": seen_count,
                "sentinel_sahne_tarih_gecmisi": dates,
                "sentinel_item_gecmisi": items,
                "son_gorulme_tarihi": source_date or track.get("son_gorulme_tarihi"),
                "son_sentinel_item": source_item or track.get("son_sentinel_item"),
                "enlem": candidate["enlem"],
                "boylam": candidate["boylam"],
                "alan_m2": candidate["alan_m2"],
                "doluluk_orani": candidate.get("doluluk_orani"),
                "tarihsel_suya_yakin_piksel_orani": candidate.get("tarihsel_suya_yakin_piksel_orani"),
                "ilk_konumdan_sapma_m": round(
                    _distance_m(
                        track.get("ilk_enlem", candidate["enlem"]),
                        track.get("ilk_boylam", candidate["boylam"]),
                        candidate["enlem"],
                        candidate["boylam"],
                    ),
                    1,
                ),
                "son_esleme_mesafe_m": round(float(matched_distance or 0.0), 1),
                "kalite_kor_eslesmesi": False,
                "kalite_kor_mesafe_m": None,
            }
        )

    # Yeni sahnede tekrar SCL=su olmayan eski izleri silme. Kalite kör bir kümeye
    # denk geliyorsa belirsizliği çözülmüş sayma; açıkça ayrı kalite takibinde tut.
    for track in tracks:
        track_id = track.get("iz_id")
        if track_id in matched_ids:
            continue
        if track.get("son_sentinel_item") == source_item and source_item:
            continue
        if track.get("son_gorulme_tarihi") == source_date and not source_item:
            continue
        if track.get("guncel_scl_su_belirsizligi") is False and track.get("durum") == "ARKA_PLAN_COZULDU":
            # Eski çözülmüş kayıtları her yeni sahnede yeniden sınıflandırmak yerine
            # geçmiş olarak sabit tut; yeniden ortaya çıkarsa mekânsal eşleme canlandırır.
            continue

        blind, blind_distance = _nearest_blind(track, blind_clusters)
        track["guncel_scl_su_belirsizligi"] = False
        track["tekrarli_scl_su_belirsizligi"] = bool(
            int(track.get("farkli_sentinel_sahnesi_gorulme_sayisi") or 0) >= 2
        )
        if blind is not None:
            track["durum"] = "KALITE_KOR_TAKIP"
            track["kalite_kor_eslesmesi"] = True
            track["kalite_kor_mesafe_m"] = round(float(blind_distance or 0.0), 1)
            track["kalite_kor_nedeni"] = blind.get("neden")
        else:
            track["durum"] = "ARKA_PLAN_COZULDU"
            track["kalite_kor_eslesmesi"] = False
            track["kalite_kor_mesafe_m"] = None
            track["kalite_kor_nedeni"] = None
            track["cozulme_sentinel_tarihi"] = source_date or None
            track["cozulme_sentinel_item"] = source_item or None

    tracks.sort(
        key=lambda row: (
            row.get("durum") == "ARKA_PLAN_COZULDU",
            row.get("durum") == "KALITE_KOR_TAKIP",
            -int(row.get("farkli_sentinel_sahnesi_gorulme_sayisi") or 0),
            float(row.get("enlem") or 0),
            float(row.get("boylam") or 0),
        )
    )

    active = [row for row in tracks if row.get("guncel_scl_su_belirsizligi")]
    repeated = [row for row in tracks if row.get("tekrarli_scl_su_belirsizligi")]
    blind_follow = [row for row in tracks if row.get("durum") == "KALITE_KOR_TAKIP"]
    resolved = [row for row in tracks if row.get("durum") == "ARKA_PLAN_COZULDU"]
    micro_active = [
        row for row in active
        if MICRO_RANGE_M2[0] <= int(row.get("alan_m2") or 0) <= MICRO_RANGE_M2[1]
    ]
    main_active = [row for row in active if int(row.get("alan_m2") or 0) >= MAIN_THRESHOLD_M2]

    return {
        "alarm": False,
        "saha_gorevi": False,
        "15_eylul_otomatik_terfi": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "esleme_mesafesi_m": TRACK_MATCH_RADIUS_M,
        "kalite_kor_esleme_mesafesi_m": BLIND_MATCH_RADIUS_M,
        "son_degerlendirilen_sentinel_tarihi": source_date,
        "son_degerlendirilen_sentinel_item": source_item,
        "ayni_sahne_idempotent": False,
        "guncel_belirsiz_iz": len(active),
        "guncel_mikro_150_249": len(micro_active),
        "guncel_ana_250plus": len(main_active),
        "farkli_sahnede_tekrarli_iz": len(repeated),
        "kalite_kor_takip": len(blind_follow),
        "arka_plan_cozuldu": len(resolved),
        "toplam_hafiza_izi": len(tracks),
        "izler": tracks,
        "yorum": (
            "Bu hafıza katmanı tarihsel kara -> güncel SCL=su çelişkisini sahneler "
            "arasında kaybetmez. Tekrar, kalite-kör takip veya çözülme yalnız diagnostik "
            "durumdur; hiçbiri inşaat/kazı kabulü, alarm, saha görevi veya 15 Eylül "
            "otomatik terfisi değildir."
        ),
    }


def _self_check():
    scene1 = {
        "kaynak_son_tarih": "08.09.2026",
        "kaynak_son_item": "S2_08",
        "adaylar": [
            {
                "enlem": 38.330000,
                "boylam": 26.650000,
                "alan_m2": 400,
                "doluluk_orani": 1.0,
                "tarihsel_suya_yakin_piksel_orani": 0.0,
                "sinif": "IC_KARA_SCL_SU_BELIRSIZLIGI",
                "yeniden_goruntule": True,
            },
            {
                "enlem": 38.340000,
                "boylam": 26.640000,
                "alan_m2": 800,
                "doluluk_orani": 1.0,
                "tarihsel_suya_yakin_piksel_orani": 0.0,
                "sinif": "IC_KARA_SCL_SU_BELIRSIZLIGI",
                "yeniden_goruntule": True,
            },
        ],
    }
    first = build_watchlist(scene1, {"guncel_sahne_kor_kumeleri": []})
    assert first["guncel_belirsiz_iz"] == 2
    assert first["farkli_sahnede_tekrarli_iz"] == 0
    assert first["alarm"] is False and first["saha_gorevi"] is False

    # Aynı sahne tekrarında sayaç artmamalı.
    same = build_watchlist(scene1, {"guncel_sahne_kor_kumeleri": []}, first)
    assert same["ayni_sahne_idempotent"] is True
    assert max(row["farkli_sentinel_sahnesi_gorulme_sayisi"] for row in same["izler"]) == 1

    # İlk iz ~22 m kayarak tekrar görülüyor; ikinci iz kayboluyor ama kalite kör.
    scene2 = {
        "kaynak_son_tarih": "11.09.2026",
        "kaynak_son_item": "S2_11",
        "adaylar": [
            {
                "enlem": 38.330180,
                "boylam": 26.650080,
                "alan_m2": 500,
                "doluluk_orani": 0.8,
                "tarihsel_suya_yakin_piksel_orani": 0.0,
                "sinif": "IC_KARA_SCL_SU_BELIRSIZLIGI",
                "yeniden_goruntule": True,
            }
        ],
    }
    blind = {
        "guncel_sahne_kor_kumeleri": [
            {"enlem": 38.340050, "boylam": 26.640000, "alan_m2": 1200, "neden": "BULUT"}
        ]
    }
    second = build_watchlist(scene2, blind, first)
    assert second["guncel_belirsiz_iz"] == 1
    assert second["farkli_sahnede_tekrarli_iz"] == 1
    assert second["kalite_kor_takip"] == 1
    repeated = next(row for row in second["izler"] if row["guncel_scl_su_belirsizligi"])
    assert repeated["farkli_sentinel_sahnesi_gorulme_sayisi"] == 2
    assert repeated["durum"] == "TEKRARLI_SCL_SU_BELIRSIZLIGI"

    # Üçüncü sahnede hiçbir iz SCL=su değil ve kalite kör de yok: geçmiş silinmez,
    # çözülmüş arka plan olarak tutulur.
    scene3 = {"kaynak_son_tarih": "13.09.2026", "kaynak_son_item": "S2_13", "adaylar": []}
    third = build_watchlist(scene3, {"guncel_sahne_kor_kumeleri": []}, second)
    assert third["guncel_belirsiz_iz"] == 0
    assert third["arka_plan_cozuldu"] == 2
    assert third["toplam_hafiza_izi"] == 2
    assert all(row["alarm"] is False and row["saha_gorevi"] is False for row in third["izler"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if args.check_only:
        print(
            "Gülbahçe kara-su geçiş hafızası öz testi başarılı; aynı-sahne idempotensi, "
            "farklı-sahne tekrar, kalite-kör takip ve çözülmüş arka-plan ayrımı doğrulandı."
        )
        return

    audit = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
    blind = json.loads(BLIND_JSON.read_text(encoding="utf-8")) if BLIND_JSON.exists() else {}
    previous = json.loads(OUTPUT_JSON.read_text(encoding="utf-8")) if OUTPUT_JSON.exists() else {}
    result = build_watchlist(audit, blind, previous)
    OUTPUT_JSON.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Gülbahçe kara-su geçiş hafızası: "
        f"güncel={result['guncel_belirsiz_iz']}, "
        f"tekrarlı={result['farkli_sahnede_tekrarli_iz']}, "
        f"kalite-kör={result['kalite_kor_takip']}, "
        f"çözülmüş={result['arka_plan_cozuldu']}. Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
