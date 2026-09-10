"""Gülbahçe kara->SCL su geçişlerini Sentinel sahneleri arasında hafızada tutar.

Bu katman yalnız diagnostiktir. Alarm veya saha görevi üretmez, 250 m² ana üretim
eşiğini değiştirmez ve 150-249 m² MİKRO adaylarını otomatik yükseltmez.

Amaç, tarihsel olarak kara olduğu doğrulanmış bir alanın güncel Sentinel SCL sahnesinde
su olarak etiketlenmesi halinde bu çelişkinin sonraki sahnede sessizce kaybolmasını
önlemektir. Kıyı/su arka planı ve geniş su-yüzey hareketleri de alarmdan ayrı tutulur
ama sahneler arasında diagnostik hafızada korunur. Aynı geçiş farklı Sentinel
sahnelerinde tekrar ederse tekrar kanıtı saklanır; sonraki sahne kalite-kör ise iz
silinmez; açık sahnede SCL-su geçişi tekrar görülmezse geçmiş kayıt arka planda
çözülmüş olarak saklanır. Hiçbir durum inşaat/kazı kabulü değildir.
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
SCHEMA_VERSION = 2
TRACKED_CLASSES = {
    "IC_KARA_SCL_SU_BELIRSIZLIGI",
    "KIYI_SU_ARKA_PLAN",
    "GENIS_SU_YUZEY_ARKA_PLAN",
}


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


def _is_inland_reimage(candidate):
    return bool(
        candidate.get("sinif") == "IC_KARA_SCL_SU_BELIRSIZLIGI"
        and candidate.get("yeniden_goruntule")
    )


def _candidate_status(candidate, seen_count):
    source_class = candidate.get("sinif")
    if _is_inland_reimage(candidate):
        return (
            "TEKRARLI_SCL_SU_BELIRSIZLIGI"
            if int(seen_count or 0) >= 2
            else "GUNCEL_SCL_SU_BELIRSIZLIGI"
        )
    if source_class == "KIYI_SU_ARKA_PLAN":
        return "KIYI_SU_ARKA_PLAN_TAKIP"
    if source_class == "GENIS_SU_YUZEY_ARKA_PLAN":
        return "GENIS_SU_YUZEY_ARKA_PLAN_TAKIP"
    return "IC_KARA_KOMPAKT_OLMAYAN_ARKA_PLAN_TAKIP"


def _current_candidates(audit):
    """150 m²+ tüm kara->SCL-su sınıflarını diagnostik hafızaya al.

    Yalnız kompakt iç-kara sınıfı ``guncel_scl_su_belirsizligi`` olarak işaretlenir.
    Kıyı, geniş yüzey ve kompakt olmayan iç-kara geçişleri yalnız arka plan izidir.
    """
    rows = []
    for row in audit.get("adaylar") or []:
        if row.get("sinif") not in TRACKED_CLASSES:
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


def _track_source_class(track):
    source_class = track.get("kaynak_sinif")
    if source_class in TRACKED_CLASSES:
        return source_class
    # Şema v1 yalnız kompakt iç-kara belirsizliklerini tutuyordu. Şema yükseltmesinde
    # eski izleri yanlışlıkla kıyı/geniş sınıfıyla eşlememek için bunu açıkça çıkar.
    if track.get("guncel_scl_su_belirsizligi") is True:
        return "IC_KARA_SCL_SU_BELIRSIZLIGI"
    return None


def _nearest_unmatched(candidate, tracks, unmatched_ids):
    best_id = None
    best_distance = None
    candidate_class = candidate.get("sinif")
    for track in tracks:
        track_id = track.get("iz_id")
        if track_id not in unmatched_ids:
            continue
        track_class = _track_source_class(track)
        if track_class and candidate_class and track_class != candidate_class:
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
    source_class = candidate.get("sinif")
    active_uncertainty = _is_inland_reimage(candidate)
    return {
        "iz_id": _track_id(candidate["enlem"], candidate["boylam"]),
        "alarm": False,
        "saha_gorevi": False,
        "15_eylul_otomatik_terfi": False,
        "durum": _candidate_status(candidate, 1),
        "kaynak_sinif": source_class,
        "sinif_gecmisi": [source_class] if source_class else [],
        "guncel_scl_su_gecisi": True,
        "guncel_scl_su_belirsizligi": active_uncertainty,
        "arka_plan_takip": not active_uncertainty,
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
            "Tarihsel kara ile güncel SCL=su geçişi diagnostik hafızada tutulur. "
            "Kıyı/geniş yüzey sınıfları yalnız arka plandır; hiçbir kayıt inşaat/kazı "
            "kanıtı değildir ve alarm/saha görevi üretmez."
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

    # Aynı Sentinel sahnesinde tekrar çalışmak sahne sayısını artırmamalı. Şema
    # yükseldiyse aynı sahneyi bir kez yeniden kurarak daha önce tutulmayan kıyı/geniş
    # arka plan izlerini hafızaya al.
    if same_scene and tracks and int(previous.get("sema_surumu") or 0) == SCHEMA_VERSION:
        result = deepcopy(previous)
        result["alarm"] = False
        result["saha_gorevi"] = False
        result["ana_uretim_esigi_m2"] = MAIN_THRESHOLD_M2
        result["mikro_aralik_m2"] = MICRO_RANGE_M2
        result["sema_surumu"] = SCHEMA_VERSION
        result["ayni_sahne_idempotent"] = True
        return result

    unmatched_ids = {row.get("iz_id") for row in tracks if row.get("iz_id")}
    matched_ids = set()

    for candidate in current:
        matched_id, matched_distance = _nearest_unmatched(candidate, tracks, unmatched_ids)
        if matched_id is None:
            track = _new_track(candidate, source_date, source_item)
            # Aynı koordinattan daha önce çözülmüş bir iz olsa bile 75 m eşleme bunu
            # yakalardı; burada yeni iz gerçekten yeni mekânsal geçiştir.
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

        source_class = candidate.get("sinif")
        class_history = list(track.get("sinif_gecmisi") or [])
        if source_class and source_class not in class_history:
            class_history.append(source_class)
        active_uncertainty = _is_inland_reimage(candidate)

        track.update(
            {
                "alarm": False,
                "saha_gorevi": False,
                "15_eylul_otomatik_terfi": False,
                "durum": _candidate_status(candidate, seen_count),
                "kaynak_sinif": source_class,
                "sinif_gecmisi": class_history,
                "guncel_scl_su_gecisi": True,
                "guncel_scl_su_belirsizligi": active_uncertainty,
                "arka_plan_takip": not active_uncertainty,
                "tekrarli_scl_su_belirsizligi": bool(active_uncertainty and seen_count >= 2),
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
    # denk geliyorsa geçişi çözülmüş sayma; açıkça ayrı kalite takibinde tut.
    for track in tracks:
        track_id = track.get("iz_id")
        if track_id in matched_ids:
            continue
        if track.get("son_sentinel_item") == source_item and source_item:
            continue
        if track.get("son_gorulme_tarihi") == source_date and not source_item:
            continue
        if track.get("guncel_scl_su_gecisi") is False and track.get("durum") == "ARKA_PLAN_COZULDU":
            # Eski çözülmüş kayıtları her yeni sahnede yeniden sınıflandırmak yerine
            # geçmiş olarak sabit tut; yeniden ortaya çıkarsa mekânsal eşleme canlandırır.
            continue
        # Eski şemada genel geçiş bayrağı yoktu; çözülmüş davranışı koru.
        if (
            "guncel_scl_su_gecisi" not in track
            and track.get("guncel_scl_su_belirsizligi") is False
            and track.get("durum") == "ARKA_PLAN_COZULDU"
        ):
            continue

        blind, blind_distance = _nearest_blind(track, blind_clusters)
        track["guncel_scl_su_gecisi"] = False
        track["guncel_scl_su_belirsizligi"] = False
        track["arka_plan_takip"] = False
        track["tekrarli_scl_su_belirsizligi"] = bool(
            track.get("tekrarli_scl_su_belirsizligi")
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
            row.get("arka_plan_takip") is True,
            -int(row.get("farkli_sentinel_sahnesi_gorulme_sayisi") or 0),
            float(row.get("enlem") or 0),
            float(row.get("boylam") or 0),
        )
    )

    current_transitions = [row for row in tracks if row.get("guncel_scl_su_gecisi") is True]
    active = [row for row in tracks if row.get("guncel_scl_su_belirsizligi") is True]
    background_active = [
        row for row in current_transitions
        if row.get("arka_plan_takip") is True
    ]
    repeated = [row for row in tracks if row.get("tekrarli_scl_su_belirsizligi")]
    repeated_transition = [
        row for row in tracks
        if int(row.get("farkli_sentinel_sahnesi_gorulme_sayisi") or 0) >= 2
    ]
    blind_follow = [row for row in tracks if row.get("durum") == "KALITE_KOR_TAKIP"]
    resolved = [row for row in tracks if row.get("durum") == "ARKA_PLAN_COZULDU"]
    micro_active = [
        row for row in active
        if MICRO_RANGE_M2[0] <= int(row.get("alan_m2") or 0) <= MICRO_RANGE_M2[1]
    ]
    main_active = [row for row in active if int(row.get("alan_m2") or 0) >= MAIN_THRESHOLD_M2]
    coastal_background = [
        row for row in background_active if row.get("kaynak_sinif") == "KIYI_SU_ARKA_PLAN"
    ]
    wide_background = [
        row for row in background_active
        if row.get("kaynak_sinif") == "GENIS_SU_YUZEY_ARKA_PLAN"
    ]
    inland_noncompact_background = [
        row for row in background_active
        if row.get("kaynak_sinif") == "IC_KARA_SCL_SU_BELIRSIZLIGI"
    ]

    return {
        "alarm": False,
        "saha_gorevi": False,
        "15_eylul_otomatik_terfi": False,
        "ana_uretim_esigi_m2": MAIN_THRESHOLD_M2,
        "mikro_aralik_m2": MICRO_RANGE_M2,
        "sema_surumu": SCHEMA_VERSION,
        "esleme_mesafesi_m": TRACK_MATCH_RADIUS_M,
        "kalite_kor_esleme_mesafesi_m": BLIND_MATCH_RADIUS_M,
        "son_degerlendirilen_sentinel_tarihi": source_date,
        "son_degerlendirilen_sentinel_item": source_item,
        "ayni_sahne_idempotent": False,
        "guncel_scl_su_gecis_150plus": len(current_transitions),
        "guncel_belirsiz_iz": len(active),
        "guncel_mikro_150_249": len(micro_active),
        "guncel_ana_250plus": len(main_active),
        "guncel_arka_plan_iz": len(background_active),
        "guncel_kiyi_arka_plan": len(coastal_background),
        "guncel_genis_su_arka_plan": len(wide_background),
        "guncel_ic_kompakt_olmayan_arka_plan": len(inland_noncompact_background),
        "farkli_sahnede_tekrarli_iz": len(repeated),
        "farkli_sahnede_tekrarli_gecis_izi": len(repeated_transition),
        "kalite_kor_takip": len(blind_follow),
        "arka_plan_cozuldu": len(resolved),
        "toplam_hafiza_izi": len(tracks),
        "izler": tracks,
        "yorum": (
            "Bu hafıza katmanı tarihsel kara -> güncel SCL=su geçişlerini sahneler arasında "
            "kaybetmez. Kompakt iç-kara belirsizliği ile kıyı/geniş yüzey arka planı ayrı "
            "durumlarda tutulur. Tekrar, kalite-kör takip veya çözülme yalnız diagnostiktir; "
            "hiçbiri inşaat/kazı kabulü, alarm, saha görevi veya 15 Eylül otomatik terfisi değildir."
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
            {
                "enlem": 38.350000,
                "boylam": 26.630000,
                "alan_m2": 400,
                "doluluk_orani": 1.0,
                "tarihsel_suya_yakin_piksel_orani": 1.0,
                "sinif": "KIYI_SU_ARKA_PLAN",
                "yeniden_goruntule": False,
            },
            {
                "enlem": 38.360000,
                "boylam": 26.620000,
                "alan_m2": 7000,
                "doluluk_orani": 0.7,
                "tarihsel_suya_yakin_piksel_orani": 0.2,
                "sinif": "GENIS_SU_YUZEY_ARKA_PLAN",
                "yeniden_goruntule": False,
            },
        ],
    }
    first = build_watchlist(scene1, {"guncel_sahne_kor_kumeleri": []})
    assert first["guncel_belirsiz_iz"] == 2
    assert first["guncel_arka_plan_iz"] == 2
    assert first["guncel_scl_su_gecis_150plus"] == 4
    assert first["toplam_hafiza_izi"] == 4
    assert first["farkli_sahnede_tekrarli_iz"] == 0
    assert first["alarm"] is False and first["saha_gorevi"] is False

    # Aynı sahne tekrarında sayaç artmamalı.
    same = build_watchlist(scene1, {"guncel_sahne_kor_kumeleri": []}, first)
    assert same["ayni_sahne_idempotent"] is True
    assert max(row["farkli_sentinel_sahnesi_gorulme_sayisi"] for row in same["izler"]) == 1

    # İlk iç-kara izi ~22 m kayarak tekrar görülüyor; ikinci iz kayboluyor ama kalite kör.
    # Kıyı ve geniş arka plan izleri görünmese de hafızadan silinmemeli.
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
    assert second["guncel_arka_plan_iz"] == 0
    assert second["farkli_sahnede_tekrarli_iz"] == 1
    assert second["kalite_kor_takip"] == 1
    assert second["arka_plan_cozuldu"] == 2
    assert second["toplam_hafiza_izi"] == 4
    repeated = next(row for row in second["izler"] if row["guncel_scl_su_belirsizligi"])
    assert repeated["farkli_sentinel_sahnesi_gorulme_sayisi"] == 2
    assert repeated["durum"] == "TEKRARLI_SCL_SU_BELIRSIZLIGI"

    # Üçüncü sahnede hiçbir iz SCL=su değil ve kalite kör de yok: geçmiş silinmez,
    # çözülmüş arka plan olarak tutulur.
    scene3 = {"kaynak_son_tarih": "13.09.2026", "kaynak_son_item": "S2_13", "adaylar": []}
    third = build_watchlist(scene3, {"guncel_sahne_kor_kumeleri": []}, second)
    assert third["guncel_belirsiz_iz"] == 0
    assert third["guncel_scl_su_gecis_150plus"] == 0
    assert third["arka_plan_cozuldu"] == 4
    assert third["toplam_hafiza_izi"] == 4
    assert all(row["alarm"] is False and row["saha_gorevi"] is False for row in third["izler"])

    # Eski şema aynı sahnede yeniden işlenirken yalnız iç-kara kayıtlarıyla yetinmemeli;
    # kıyı/geniş arka plan izlerini eksiksiz eklemeli ve gözlem sayısını artırmamalı.
    old_schema = deepcopy(first)
    old_schema.pop("sema_surumu", None)
    old_schema["izler"] = [
        row for row in old_schema["izler"]
        if row.get("kaynak_sinif") == "IC_KARA_SCL_SU_BELIRSIZLIGI"
    ]
    upgraded = build_watchlist(scene1, {"guncel_sahne_kor_kumeleri": []}, old_schema)
    assert upgraded["sema_surumu"] == SCHEMA_VERSION
    assert upgraded["guncel_scl_su_gecis_150plus"] == 4
    assert upgraded["guncel_arka_plan_iz"] == 2
    assert upgraded["toplam_hafiza_izi"] == 4
    assert max(row["farkli_sentinel_sahnesi_gorulme_sayisi"] for row in upgraded["izler"]) == 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    _self_check()
    if args.check_only:
        print(
            "Gülbahçe kara-su geçiş hafızası öz testi başarılı; aynı-sahne idempotensi, "
            "kıyı/geniş arka plan kalıcılığı, farklı-sahne tekrar, kalite-kör takip ve "
            "çözülmüş arka-plan ayrımı doğrulandı."
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
        f"güncel-geçiş={result['guncel_scl_su_gecis_150plus']}, "
        f"iç-kara-belirsiz={result['guncel_belirsiz_iz']}, "
        f"arka-plan={result['guncel_arka_plan_iz']}, "
        f"tekrarlı={result['farkli_sahnede_tekrarli_gecis_izi']}, "
        f"kalite-kör={result['kalite_kor_takip']}, "
        f"çözülmüş={result['arka_plan_cozuldu']}. Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
