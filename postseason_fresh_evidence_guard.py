"""15 Eylül sonrası taze Sentinel kanıtının saatlik yenilemede erken sönmesini önler.

Ana taze-kazı koruması bir adayın ilk işlendiği turda ``yeni_goruntu=True`` olmasını
ister. Bu doğru bir ilk-giriş kilididir; ancak aynı Sentinel sahnesi sonraki rapor
yenilemelerinde yeni sayılmadığında, gerçekten yeni başlamış bir hafriyatın operasyon
önceliği birkaç saat içinde kaybolmamalıdır.

Bu katman yeni alarm veya saha görevi üretmez. Yalnız 15 Eylül 2026 ve sonrasında ilk
kez görülen, halen güncel Sentinel kümesinde bulunan ve uydu kanıt yaşı en fazla iki
gün olan güçlü 250–5.000 m² ana-alarm adaylarının taze-kazı rota önceliğini kısa süre
korur. 150–249 m² MİKRO ŞANTİYE diagnostikleri hiçbir koşulda yükseltilmez; tarihsel
kanıt, geniş-geometri/arka-plan işareti ve açık alarm/görev dışı kayıtlar dışarıda
kalır.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json

import postseason_excavation_priority_guard as base


RETENTION_DAYS = 2
RETENTION_NOTE = (
    "15 Eylül sonrası operasyon modu: yeni Sentinel görüntüsünde beliren güçlü 250 m²+ "
    "hafriyat/temel adayları ve ilk görülmesinden sonra en fazla 2 günlük güncel Sentinel "
    "kanıtı taşıyan aynı adaylar eski backlog'un önünde tutulur. TEKRAR_GIT her zaman "
    "en yüksek önceliktedir. 150–249 m² Mikro Şantiye diagnostikleri doğrudan saha "
    "görevine yükseltilmez."
)

_ORIGINAL_FRESH_CLASSIFIER = base._is_fresh_excavation_candidate


def _day_value(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _int_value(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return int(number)


def _retained_fresh_candidate(item, local_day=None):
    """Yeni-görüntü biti söndükten sonra kısa süre korunabilecek güçlü adayı tanı."""
    if not isinstance(item, dict) or base._is_manual_repeat(item):
        return False

    day = base._local_day(local_day)
    if day < base.FULL_OPERATION_START:
        return False

    area = max(base._number(item.get("alan_m2"), 0.0), 0.0)
    if not (base.MAIN_ALARM_MIN_M2 <= area <= base.FRESH_EXCAVATION_MAX_M2):
        return False

    # İlk tur zaten ana guard tarafından yükseltilir; bu katman yalnız sonraki
    # yenilemelerde yeni_goruntu biti söndüğünde devreye girer.
    if item.get("yeni_goruntu") is True:
        return False

    first_seen = _day_value(item.get("ilk_gorulme"))
    if first_seen is None or first_seen < base.FULL_OPERATION_START or first_seen > day:
        return False

    evidence_day = _day_value(item.get("son_tarih"))
    if evidence_day is None or evidence_day < base.FULL_OPERATION_START or evidence_day > day:
        return False

    evidence_age = _int_value(item.get("uydu_kanit_yasi_gun"))
    if evidence_age is None or evidence_age > RETENTION_DAYS:
        return False

    # Son analizde tekrar görünmeyip yalnız tarihsel ölçüsü taşınan kayıt taze sayılamaz.
    if base.freshness._historical_evidence_rank(item) != 0:
        return False

    if item.get("genis_geometri_riski") is True:
        return False
    if str(item.get("izleme") or "").strip().upper() == "ARKA_PLAN_GENIS_YUZEY":
        return False
    if item.get("alarm") is False or item.get("saha_gorevi") is False:
        return False

    priority = str(item.get("oncelik") or "").strip().upper()
    if priority in base.FRESH_ROUTE_PRIORITIES:
        return True

    size_class = str(item.get("boyut_sinifi") or "").strip().upper()
    satellite_priority = str(item.get("uydu_onceligi") or "").strip().upper()
    signal = str(item.get("sinyal") or "").casefold()
    return (
        area <= base.STRONG_SMALL_MAX_M2
        and (size_class == "KUCUK" or "küçük, güçlü" in signal)
        and satellite_priority in base.ALLOWED_SATELLITE_PRIORITIES
    )


def _fresh_with_retention(item, local_day=None):
    return _ORIGINAL_FRESH_CLASSIFIER(item) or _retained_fresh_candidate(item, local_day)


def select_shortlist(candidates, limit=base.route.SHORTLIST_LIMIT, local_day=None):
    """Ana sıralama/bölge dengelemesini aynı sınıflandırıcıya retention ekleyerek kullan."""
    day = base._local_day(local_day)
    original = base._is_fresh_excavation_candidate
    try:
        base._is_fresh_excavation_candidate = lambda item: _fresh_with_retention(item, day)
        return base.select_postseason_shortlist(candidates, limit=limit, local_day=day)
    finally:
        base._is_fresh_excavation_candidate = original


def _markdown(shortlist, local_day=None):
    day = base._local_day(local_day)
    original = base._is_fresh_excavation_candidate
    try:
        base._is_fresh_excavation_candidate = lambda item: _fresh_with_retention(item, day)
        text = base._shortlist_markdown(shortlist)
    finally:
        base._is_fresh_excavation_candidate = original
    return text.replace(base.NOTE, RETENTION_NOTE, 1)


def apply_retention(local_day=None):
    day = base._local_day(local_day)
    if day < base.FULL_OPERATION_START:
        return False, []

    payload = json.loads(base.route.REPORT_JSON.read_text(encoding="utf-8"))
    candidates = payload.get("saha_adaylari") or []
    shortlist = select_shortlist(candidates, local_day=day)
    retained_ids = [
        str(item.get("gorev_id") or "")
        for item in shortlist
        if _retained_fresh_candidate(item, day)
    ]

    payload["gunun_ilk_3_kontrolu"] = shortlist
    payload["gunun_ilk_3_notu"] = RETENTION_NOTE
    payload["postseason_fresh_evidence_retention"] = {
        "aktif": True,
        "baslangic_tarihi": base.FULL_OPERATION_START.isoformat(),
        "koruma_suresi_gun": RETENTION_DAYS,
        "ana_alarm_alt_esigi_m2": base.MAIN_ALARM_MIN_M2,
        "taze_kazi_oncelik_ust_siniri_m2": base.FRESH_EXCAVATION_MAX_M2,
        "korunan_gorevler": retained_ids,
        "mikro_santiye": "150-249 m² diagnostik; doğrudan saha görevine yükseltilmez",
        "not": (
            "Yeni alarm/görev üretmez. Yalnız 15 Eylül ve sonrasında ilk kez görülen, "
            "tarihsel olmayan, en fazla 2 günlük güncel Sentinel kanıtlı güçlü ana-alarm "
            "adayının rota önceliğini saatlik rapor yenilemeleri arasında korur."
        ),
    }

    before_json = base.route.REPORT_JSON.read_text(encoding="utf-8")
    after_json = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    changed = before_json != after_json
    if changed:
        base.route.REPORT_JSON.write_text(after_json, encoding="utf-8")

    if base.route.FIELD_REPORT_MD.exists():
        current = base.route.FIELD_REPORT_MD.read_text(encoding="utf-8")
        updated = base.route._inject_markdown(current, _markdown(shortlist, day))
        if updated != current:
            base.route.FIELD_REPORT_MD.write_text(updated, encoding="utf-8")
            changed = True

    return changed, shortlist


def _candidate(task_id, **updates):
    item = {
        "gorev_id": task_id,
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "ERKEN",
        "mahalle": "Gülbahçe",
        "enlem": 38.3328,
        "boylam": 26.6456,
        "alan_m2": 600,
        "bolge": base.freshness.CANONICAL_EAST_REGION,
        "yeni_goruntu": False,
        "ilk_gorulme": "2026-09-15",
        "son_tarih": "15.09.2026",
        "uydu_kanit_yasi_gun": 1,
        "uydu_onceligi": "YÜKSEK",
        "boyut_sinifi": "KUCUK",
        "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
    }
    item.update(updates)
    return item


def _self_check():
    day = date(2026, 9, 16)
    retained = _candidate("RETAINED")
    assert _retained_fresh_candidate(retained, day)

    assert not _retained_fresh_candidate(
        _candidate("PREBAN", ilk_gorulme="2026-09-14", son_tarih="14.09.2026"), day
    )
    assert not _retained_fresh_candidate(
        _candidate("PREBAN_SCENE", ilk_gorulme="2026-09-15", son_tarih="14.09.2026"), day
    )
    assert not _retained_fresh_candidate(
        _candidate("STALE", uydu_kanit_yasi_gun=RETENTION_DAYS + 1), day
    )
    assert not _retained_fresh_candidate(
        _candidate("HISTORICAL", tarihsel_esleme_mesafe_m=8.0), day
    )
    assert not _retained_fresh_candidate(
        _candidate("MICRO", alan_m2=200), day
    )
    assert not _retained_fresh_candidate(
        _candidate("BACKGROUND", izleme="ARKA_PLAN_GENIS_YUZEY"), day
    )
    assert not _retained_fresh_candidate(
        _candidate("NO_TASK", saha_gorevi=False), day
    )

    repeat = _candidate(
        "REPEAT",
        saha_durumu="TEKRAR_GIT",
        oncelik="TEKRAR",
        yeni_goruntu=False,
        ilk_gorulme="2026-09-10",
        uydu_kanit_yasi_gun=6,
    )
    live = _candidate("LIVE", yeni_goruntu=True, ilk_gorulme="2026-09-16", uydu_kanit_yasi_gun=0)
    backlog = _candidate(
        "BACKLOG",
        ilk_gorulme="2026-09-01",
        uydu_kanit_yasi_gun=15,
        tarihsel_esleme_mesafe_m=4.0,
    )
    selected = select_shortlist([backlog, retained, live, repeat], limit=3, local_day=day)
    ids = [item["gorev_id"] for item in selected]
    assert ids[0] == "REPEAT", ids
    assert "LIVE" in ids[:3], ids
    assert "RETAINED" in ids[:3], ids
    assert "BACKLOG" not in ids[:3], ids

    # Ana eşikler sabit; Mikro doğrudan saha görevine çıkamaz.
    assert base.MAIN_ALARM_MIN_M2 == 250
    assert base.MICRO_MAX_M2 == 249
    print("OK: 15 Eylül taze Sentinel kanıtı 2 günlük retention self-check geçti.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        _self_check()
        return

    changed, shortlist = apply_retention()
    if base._local_day() < base.FULL_OPERATION_START:
        print("Kalibrasyon modu: 15 Eylül öncesi rapora dokunulmadı.")
        return
    retained = sum(_retained_fresh_candidate(item) for item in shortlist)
    print(f"Taze Sentinel kanıt retention uygulandı: ilk üçte {retained} korunan aday.")
    if not changed:
        print("Rapor zaten güncel.")


if __name__ == "__main__":
    main()
