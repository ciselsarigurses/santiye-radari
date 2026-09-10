"""15 Eylül taze-kazı sıralamasında Sentinel sahne tarihini sezon sınırına kilitler.

Bu katman yalnız mevcut saha görevlerinin sırasını korur. Yeni alarm/görev üretmez,
250 m² ana Sentinel eşiğini değiştirmez ve 150–249 m² MİKRO ŞANTİYE katmanını
operasyonel göreve yükseltmez.

Ana sezon guard'ı ``yeni_goruntu=True`` olan güçlü 250 m²+ adayı taze kabul eder.
15 Eylül sabahı son kullanılabilir sahne 14 Eylül'e aitse bu bit hâlâ doğru olabilir;
ancak o sinyal sezon açıldıktan sonra başlamış yeni hafriyat değildir. Bu guard ilk-tur
TAZE KAZI bandını yalnız sahne tarihi 15 Eylül 2026 veya daha yeni ve en fazla iki
günlükse kabul eder. Daha eski aday silinmez; normal/backlog kanıtı olarak izlenir.
İki günlük ``postseason_fresh_evidence_guard`` retention mantığı aynen korunur.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date
import json
from pathlib import Path

import postseason_excavation_priority_guard as base
import postseason_fresh_evidence_guard as retention
import postseason_dry_ground_retention_guard as dry_bridge


ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "latest_report.json"
SEASON_START = base.FULL_OPERATION_START
MAX_FIRST_SCENE_AGE_DAYS = retention.RETENTION_DAYS
MAIN_MIN = base.MAIN_ALARM_MIN_M2
MICRO_MIN = base.MICRO_MIN_M2
MICRO_MAX = base.MICRO_MAX_M2
_BASE_FIRST_FRESH_CLASSIFIER = base._is_fresh_excavation_candidate


def _scene_date(item):
    if not isinstance(item, dict):
        return None
    return base._parse_scene_date(item.get("son_tarih"))


def _guarded_first_fresh(item, local_day=None):
    """İlk taze-kazı bandını sezon başlangıcı + sahne yaşı ile sınırla."""
    if not _BASE_FIRST_FRESH_CLASSIFIER(item):
        return False
    day = base._local_day(local_day)
    scene_day = _scene_date(item)
    if scene_day is None or scene_day < SEASON_START or scene_day > day:
        return False
    return (day - scene_day).days <= MAX_FIRST_SCENE_AGE_DAYS


def _blocked_reason(item, local_day=None):
    """Eski sınıflayıcı taze derken tarih guard'ının neden reddettiğini açıkla."""
    if not _BASE_FIRST_FRESH_CLASSIFIER(item):
        return None
    day = base._local_day(local_day)
    scene_day = _scene_date(item)
    if scene_day is None:
        return "SAHNE_TARIHI_YOK"
    if scene_day < SEASON_START:
        return "SEZON_ONCESI_SAHNE"
    if scene_day > day:
        return "GELECEK_TARIHLI_SAHNE"
    if (day - scene_day).days > MAX_FIRST_SCENE_AGE_DAYS:
        return "BAYAT_YENI_GORUNTU_BITI"
    return None


@contextmanager
def _date_aware_classifiers(local_day=None):
    """Retention ve kuru-zemin merge'i boyunca aynı sahne-tarihi invariantını kullan."""
    day = base._local_day(local_day)
    previous_retention = retention._ORIGINAL_FRESH_CLASSIFIER
    previous_dry = dry_bridge._ORIGINAL_FRESH_CLASSIFIER
    guarded = lambda item: _guarded_first_fresh(item, day)
    retention._ORIGINAL_FRESH_CLASSIFIER = guarded
    dry_bridge._ORIGINAL_FRESH_CLASSIFIER = guarded
    try:
        yield
    finally:
        retention._ORIGINAL_FRESH_CLASSIFIER = previous_retention
        dry_bridge._ORIGINAL_FRESH_CLASSIFIER = previous_dry


def _load_report():
    try:
        payload = json.loads(REPORT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _blocked_candidates(candidates, local_day=None):
    day = base._local_day(local_day)
    blocked = []
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        reason = _blocked_reason(item, day)
        if reason is None:
            continue
        blocked.append(
            {
                "gorev_id": str(item.get("gorev_id") or ""),
                "son_tarih": str(item.get("son_tarih") or ""),
                "neden": reason,
            }
        )
    return blocked


def _stamp_metadata(local_day=None, blocked=None):
    """Tarih guard'ını görünür kıl; eski taze-kazı metadata'sını yanlış bırakma."""
    day = base._local_day(local_day)
    payload = _load_report()
    if payload is None:
        return False

    blocked = list(blocked or [])
    blocked_ids = {row["gorev_id"] for row in blocked if row.get("gorev_id")}
    priority_meta = payload.get("postseason_excavation_priority")
    if isinstance(priority_meta, dict) and blocked_ids:
        promoted = [
            str(task_id)
            for task_id in priority_meta.get("one_alinan_gorevler") or []
            if str(task_id) not in blocked_ids
        ]
        priority_meta["one_alinan_gorevler"] = promoted
        priority_meta["sahne_tarihi_guard_ile_engellenen"] = sorted(blocked_ids)

    payload["postseason_scene_freshness_guard"] = {
        "aktif": True,
        "baslangic_tarihi": SEASON_START.isoformat(),
        "ana_alarm_alt_esigi_m2": MAIN_MIN,
        "mikro_aralik_m2": [MICRO_MIN, MICRO_MAX],
        "ilk_tur_taze_sahne_maks_yasi_gun": MAX_FIRST_SCENE_AGE_DAYS,
        "engellenen_taze_kazi_siniflamalari": blocked,
        "alarm": False,
        "yeni_saha_gorevi": False,
        "not": (
            "TAZE KAZI sıralama bandı yalnız 15 Eylül 2026 veya daha yeni Sentinel "
            "sahnesine verilir. Sezon öncesi sinyal silinmez; normal/backlog kanıtı "
            "olarak izlenir. 150-249 m² MİKRO doğrudan saha görevine yükseltilmez."
        ),
    }

    before = REPORT.read_text(encoding="utf-8")
    after = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if before == after:
        return False
    REPORT.write_text(after, encoding="utf-8")
    return True


def apply_guard(local_day=None):
    day = base._local_day(local_day)
    if day < SEASON_START:
        return False, []

    payload = _load_report()
    if payload is None:
        return False, []
    candidates = payload.get("saha_adaylari") or []
    blocked = _blocked_candidates(candidates, day)

    # Önce ana+retention kısa listesini sahne tarihi guard'ıyla yeniden kur; ardından
    # kuru-zemin diagnostik merge'ini aynı classifier altında uygula ki son yazan katman
    # sezon-öncesi bir sahneyi yeniden taze banda taşımasın.
    with _date_aware_classifiers(day):
        retention_changed, _ = retention.apply_retention(local_day=day)
        dry_changed, _ = dry_bridge.apply_guard(local_day=day)

    metadata_changed = _stamp_metadata(day, blocked)
    return retention_changed or dry_changed or metadata_changed, blocked


def _candidate(task_id, scene_date, **updates):
    row = {
        "gorev_id": task_id,
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "ERKEN",
        "mahalle": "Gülbahçe",
        "enlem": 38.3315,
        "boylam": 26.6443,
        "alan_m2": 400,
        "bolge": base.freshness.CANONICAL_EAST_REGION,
        "yeni_goruntu": True,
        "son_tarih": scene_date,
        "uydu_onceligi": "ORTA",
        "boyut_sinifi": "KUCUK",
        "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
    }
    row.update(updates)
    return row


def _self_check():
    opening_day = date(2026, 9, 15)
    preopening = _candidate("PREOPENING", "14.09.2026")
    opening = _candidate("OPENING", "15.09.2026", enlem=38.34, boylam=26.65)
    missing_date = _candidate("NO_DATE", "")
    future = _candidate("FUTURE", "16.09.2026")
    stale = _candidate("STALE", "15.09.2026")
    micro = _candidate("MICRO", "15.09.2026", alan_m2=200)

    # Ana taze-kazı sınıflayıcısı artık sezon sınırını kendi içinde de kilitliyor.
    # Bu guard ikinci savunma hattı olarak sahne yaşını/future-date durumunu korur;
    # sezon öncesi örnek ana sınıflayıcıda zaten reddedildiği için blocked listesine
    # ikinci kez yazılmaz.
    assert not _BASE_FIRST_FRESH_CLASSIFIER(preopening)
    assert not _guarded_first_fresh(preopening, opening_day)
    assert _blocked_reason(preopening, opening_day) is None
    assert _guarded_first_fresh(opening, opening_day)
    assert not _guarded_first_fresh(missing_date, opening_day)
    assert not _guarded_first_fresh(future, opening_day)
    assert not _guarded_first_fresh(stale, date(2026, 9, 18))
    assert not _guarded_first_fresh(micro, opening_day)

    repeat = _candidate(
        "REPEAT", "10.09.2026", saha_durumu="TEKRAR_GIT", oncelik="TEKRAR",
        yeni_goruntu=False, enlem=38.30, boylam=26.30,
    )
    backlog = _candidate(
        "BACKLOG", "12.09.2026", oncelik="GECİKEN", yeni_goruntu=False,
        tarihsel_esleme_mesafe_m=5.0, enlem=38.31, boylam=26.31,
    )
    with _date_aware_classifiers(opening_day):
        selected = retention.select_shortlist(
            [preopening, backlog, opening, repeat],
            limit=2,
            local_day=opening_day,
            micro_watchlist={},
        )
    ids = [row.get("gorev_id") for row in selected]
    assert ids == ["REPEAT", "OPENING"], ids

    # Retention, ilk sahne biti söndükten sonra yalnız sezon-içi kanıtı korumaya devam etmeli.
    retained = _candidate(
        "RETAINED", "15.09.2026", yeni_goruntu=False,
        ilk_gorulme="2026-09-01", uydu_kanit_yasi_gun=1,
    )
    assert retention._retained_fresh_candidate(retained, date(2026, 9, 16))

    assert MAIN_MIN == 250
    assert (MICRO_MIN, MICRO_MAX) == (150, 249)
    assert retention._ORIGINAL_FRESH_CLASSIFIER is not None
    assert dry_bridge._ORIGINAL_FRESH_CLASSIFIER is not None
    print("OK: 15 Eylül taze-kazı bandı yalnız sezon-içi Sentinel sahnesine veriliyor.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        _self_check()
        return

    changed, blocked = apply_guard()
    if base._local_day() < SEASON_START:
        print("Sahne tazelik guard'ı: kalibrasyon dönemi, üretim raporuna dokunulmadı.")
        return
    print(
        "Sahne tazelik guard'ı: "
        f"engellenen yanlış taze-sınıflama={len(blocked)}, rapor güncellendi={changed}."
    )


if __name__ == "__main__":
    main()
