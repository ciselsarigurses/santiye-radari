"""Kuru-zemin DOĞRULAMA katmanının taze-kazı retention önceliğini bozmamasını sağlar.

15 Eylül sonrası ana 250 m²+ üretim adayı yeni Sentinel sahnesinde taze-kazı olarak
öne alındıktan sonra ``yeni_goruntu`` biti sonraki saatlik yenilemede sönebilir. Bu aday
``postseason_fresh_evidence_guard`` tarafından en fazla iki gün taze tutulur. Kuru-zemin
DOĞRULAMA katmanı bu retention'ı da gerçek taze ana adayla aynı koruma bandında görmelidir;
yoksa diagnostik bir DOĞRULAMA, korunmuş ana üretim adayını ilk üçten düşürebilir.

Bu köprü yalnız sıralama invariantını korur. Yeni alarm/görev üretmez, ana 250 m² eşiğini
ve 150–249 m² MİKRO politikasını değiştirmez; kuru-zemin diagnostik kurallarına dokunmaz.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date

import postseason_dry_ground_confirmation_guard as dry_ground
import postseason_excavation_priority_guard as postseason
import postseason_fresh_evidence_guard as retention


_ORIGINAL_FRESH_CLASSIFIER = postseason._is_fresh_excavation_candidate


def _is_protected_fresh(item, local_day=None):
    """İlk-tur taze aday veya iki günlük retention adayı aynı koruma bandındadır."""
    day = dry_ground._local_day(local_day)
    return _ORIGINAL_FRESH_CLASSIFIER(item) or retention._retained_fresh_candidate(item, day)


@contextmanager
def _retention_aware_classifier(local_day=None):
    """Kuru-zemin merge'i boyunca ana taze-aday sınıflayıcısını güvenle genişlet."""
    day = dry_ground._local_day(local_day)
    previous = postseason._is_fresh_excavation_candidate
    postseason._is_fresh_excavation_candidate = lambda item: _is_protected_fresh(item, day)
    try:
        yield
    finally:
        postseason._is_fresh_excavation_candidate = previous


def apply_guard(local_day=None, db_path=dry_ground.DB):
    day = dry_ground._local_day(local_day)
    with _retention_aware_classifier(day):
        return dry_ground.apply_confirmation(local_day=day, db_path=db_path)


def _self_check():
    day = date(2026, 9, 16)
    west = dry_ground.freshness.CANONICAL_WEST_REGION

    # Mevcut kuru-zemin kurallarının kendi testleri de aynı entegrasyon bağlamında geçmeli.
    with _retention_aware_classifier(day):
        dry_ground._self_check()

    repeat = {
        "gorev_id": "R",
        "saha_durumu": "TEKRAR_GIT",
        "oncelik": "TEKRAR",
        "bolge": west,
        "enlem": 38.30,
        "boylam": 26.30,
    }
    retained = {
        "gorev_id": "RETAINED_MAIN",
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "ERKEN",
        "alan_m2": 600,
        "bolge": west,
        "enlem": 38.31,
        "boylam": 26.31,
        "yeni_goruntu": False,
        "ilk_gorulme": "2026-09-01",
        "son_tarih": "15.09.2026",
        "uydu_kanit_yasi_gun": 1,
        "uydu_onceligi": "YÜKSEK",
        "boyut_sinifi": "KUCUK",
        "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
    }
    backlog = {
        "gorev_id": "BACKLOG",
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "GECİKEN",
        "alan_m2": 900,
        "bolge": west,
        "enlem": 38.32,
        "boylam": 26.33,
    }
    confirmation = {
        "gorev_id": "DG_TEST",
        "saha_durumu": "DIAGNOSTIK_DOGRULAMA",
        "oncelik": "DOĞRULAMA",
        "alan_m2": 500,
        "bolge": west,
        "enlem": 38.34,
        "boylam": 26.35,
        "alarm": False,
        "saha_gorevi": False,
        "postseason_kuru_zemin_dogrulama": True,
    }

    assert retention._retained_fresh_candidate(retained, day)
    assert not _ORIGINAL_FRESH_CLASSIFIER(retained)

    with _retention_aware_classifier(day):
        merged = dry_ground.merge_confirmation([repeat, retained, backlog], [confirmation])
    ids = [item.get("gorev_id") for item in merged]
    assert ids == ["R", "RETAINED_MAIN", "DG_TEST"], ids

    # MİKRO 150–249 m² hiçbir şekilde retention/taze ana banda çıkmamalı.
    micro = dict(retained, gorev_id="MICRO", alan_m2=200)
    assert not _is_protected_fresh(micro, day)
    assert postseason.MAIN_ALARM_MIN_M2 == 250
    assert postseason.MICRO_MIN_M2 == 150 and postseason.MICRO_MAX_M2 == 249

    # Context manager sonrasında global sınıflayıcı mutlaka eski haline dönmeli.
    assert postseason._is_fresh_excavation_candidate is _ORIGINAL_FRESH_CLASSIFIER
    print("OK: kuru-zemin DOĞRULAMA, retention ile korunan taze ana adayı düşürmüyor.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()

    if args.self_check:
        _self_check()
        return

    changed, chosen = apply_guard()
    if dry_ground._local_day() < dry_ground.FULL_OPERATION_START:
        print("15 Eylül kuru-zemin retention köprüsü: kalibrasyon dönemi, rapora dokunulmadı.")
    else:
        print(
            "15 Eylül kuru-zemin retention köprüsü: "
            f"{len(chosen)} güçlü diagnostik; rapor güncellendi={changed}."
        )


if __name__ == "__main__":
    main()
