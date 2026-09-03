"""15 Eylül sonrası şantiye-ölçeği temsil ihtiyacını yalnız diagnostik olarak ölçer.

Ana Sentinel motoru 250 m² eşiğini, 150-249 m² MİKRO ŞANTİYE politikasını ve toplam
aday/alarm sayısını değiştirmez. ``candidate_capacity_audit.json`` içindeki aynı
üretim filtresini geçmiş ham 800-10.000 m² havuzu ile güncel raporda kalan dağılımı
karşılaştırır. Amaç 15 Eylül sonrası yeni hafriyat/temel sinyallerine daha yüksek
operasyonel ağırlık vermeden önce, geniş >10.000 m² adayları bire bir değiştirerek
şantiye-ölçeği temsilini artırmanın kapasite açısından mümkün olup olmadığını ölçmek.

Bu dosyadaki 10-aday senaryosu üretim politikası değildir. Saha doğrulaması yetersizse
özellikle ``URETIME_DOKUNMA`` sonucu verir; sırf aday havuzu büyük diye kota yükseltmez.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ISTANBUL = ZoneInfo("Europe/Istanbul")
AUDIT_FILE = Path(__file__).with_name("candidate_capacity_audit.json")
REPORT_FILE = Path(__file__).with_name("latest_report.json")
OUTPUT_FILE = Path(__file__).with_name("postseason_capacity_preview.json")

MAIN_ALARM_MIN_M2 = 250
MICRO_MIN_M2 = 150
MICRO_MAX_M2 = 249
CONSTRUCTION_MIN_M2 = 800
CONSTRUCTION_MAX_M2 = 10_000
WIDE_MIN_M2 = 10_001
CURRENT_POST_DEDUPE_TARGET = 8
DIAGNOSTIC_POSTSEASON_TARGET = 10
FULL_OPERATION_START = date(2026, 9, 15)

# Saha örneklemi çok küçükken üretim kotasını değiştirmemek için yalnız bir kanıt
# yeterliliği eşiği. Bu eşik alarm/aday filtresi değildir; karar desteği içindir.
MIN_FIELD_CONTROLS_FOR_POLICY = 5
MIN_CONFIRMED_CONSTRUCTION_FOR_POLICY = 3


def _load_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _semantic_payload(payload):
    """Yalnız üretim kararını etkileyen alanları karşılaştır; üretim saatini yok say."""
    if not isinstance(payload, dict):
        return {}
    normalized = dict(payload)
    normalized.pop("olusturma", None)
    return normalized


def _write_preview_if_meaningful(payload):
    """Semantik durum değişmediyse salt zaman damgası için dosyayı yeniden yazma."""
    previous = _load_json(OUTPUT_FILE)
    if previous and _semantic_payload(previous) == _semantic_payload(payload):
        payload["olusturma"] = previous.get("olusturma") or payload.get("olusturma")
        return False

    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def _field_counts(report):
    """Günlük özetten saha kontrol ve doğrulanmış şantiye/kazı sayılarını güvenli oku."""
    summary = str(report.get("ozet") or "")
    controls = 0
    confirmed = 0
    control_match = re.search(r"Saha sonucu:\s*(\d+)\s*kontrol", summary, re.IGNORECASE)
    confirmed_match = re.search(r"\((\d+)\s*şantiye/kazı", summary, re.IGNORECASE)
    if control_match:
        controls = int(control_match.group(1))
    if confirmed_match:
        confirmed = int(confirmed_match.group(1))
    return controls, confirmed


def _region_preview(record):
    raw = record.get("ham_olcek_dagilimi") or {}
    kept = record.get("raporda_olcek_dagilimi") or {}
    raw_construction = max(int(raw.get("santiye_olcegi_800_10000") or 0), 0)
    current_construction = max(int(kept.get("santiye_olcegi_800_10000") or 0), 0)
    current_wide = max(int(kept.get("genis_10000_ustu") or 0), 0)

    missing_to_scenario = max(DIAGNOSTIC_POSTSEASON_TARGET - current_construction, 0)
    unseen_construction_pool = max(raw_construction - current_construction, 0)
    possible_one_for_one = min(
        missing_to_scenario,
        current_wide,
        unseen_construction_pool,
    )
    theoretical_after_swap = current_construction + possible_one_for_one

    return {
        "durum": str(record.get("durum") or "bilinmiyor"),
        "latest_item": record.get("latest_item") or record.get("son_item"),
        "ham_800_10000": raw_construction,
        "raporda_800_10000": current_construction,
        "raporda_10000_ustu": current_wide,
        "mevcut_post_dedupe_hedefi": CURRENT_POST_DEDUPE_TARGET,
        "diagnostik_15eylul_senaryosu": DIAGNOSTIC_POSTSEASON_TARGET,
        "senaryoya_eksik": missing_to_scenario,
        "bire_bir_takasla_teorik_ek_kapasite": possible_one_for_one,
        "teorik_son_800_10000": theoretical_after_swap,
        "senaryo_kapasite_acisindan_mumkun": (
            theoretical_after_swap >= DIAGNOSTIC_POSTSEASON_TARGET
        ),
    }


def build_preview(now=None):
    now = now or datetime.now(ISTANBUL)
    audit = _load_json(AUDIT_FILE)
    report = _load_json(REPORT_FILE)
    report_date = str(report.get("rapor_tarihi") or "")
    audit_date = str(audit.get("rapor_tarihi") or "")
    controls, confirmed = _field_counts(report)

    field_evidence_ready = (
        controls >= MIN_FIELD_CONTROLS_FOR_POLICY
        and confirmed >= MIN_CONFIRMED_CONSTRUCTION_FOR_POLICY
    )
    same_day = bool(report_date and report_date == audit_date)

    regions = {}
    for region_key, record in (audit.get("bolgeler") or {}).items():
        if not isinstance(record, dict):
            continue
        regions[str(region_key)] = _region_preview(record)

    capacity_ready = bool(regions) and all(
        item.get("durum") == "ok"
        and item.get("senaryo_kapasite_acisindan_mumkun") is True
        for item in regions.values()
    )

    if not same_day:
        recommendation = "VERI_TAZELIGINI_BEKLE"
        reason = "Kapasite denetimi ile günlük rapor aynı tarihe ait değil; üretim kararı verilmez."
    elif not field_evidence_ready:
        recommendation = "URETIME_DOKUNMA"
        reason = (
            "Saha doğrulama örneklemi politika değişikliği için yetersiz. 15 Eylül senaryosu "
            "yalnız kapasite diagnostikidir; eşik/kota/alarm değiştirilmez."
        )
    elif capacity_ready:
        recommendation = "15_EYLUL_SONRASI_KONTROLLU_KOTA_TESTI_DEGERLENDIR"
        reason = (
            "Saha kanıtı yeterli ve iki bölgede de geniş adayları bire bir değiştirerek "
            "10 şantiye-ölçeği temsil senaryosu kapasite açısından mümkün görünüyor."
        )
    else:
        recommendation = "MEVCUT_KOTAYI_KORU"
        reason = "Bir veya daha fazla bölgede 10-aday senaryosu alarm sayısını büyütmeden doldurulamıyor."

    payload = {
        "rapor_tarihi": report_date or audit_date,
        "olusturma": now.strftime("%Y-%m-%d %H:%M %z"),
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": MAIN_ALARM_MIN_M2,
        "mikro_santiye_araligi_m2": [MICRO_MIN_M2, MICRO_MAX_M2],
        "santiye_olcegi_m2": [CONSTRUCTION_MIN_M2, CONSTRUCTION_MAX_M2],
        "tam_operasyon_baslangici": FULL_OPERATION_START.isoformat(),
        "diagnostik_senaryo_notu": (
            "10 aday yalnız karar-destek senaryosudur; üretim kotası değildir. Toplam alarm "
            "sayısını artırmak yerine >10.000 m² adaylarla bire bir takas kapasitesi ölçülür."
        ),
        "saha_kaniti": {
            "kontrol": controls,
            "dogrulanmis_santiye_kazi": confirmed,
            "politika_icin_yeterli": field_evidence_ready,
            "asgari_kontrol": MIN_FIELD_CONTROLS_FOR_POLICY,
            "asgari_dogrulanmis_santiye_kazi": MIN_CONFIRMED_CONSTRUCTION_FOR_POLICY,
        },
        "veri_tazeligi": {
            "gunluk_rapor_tarihi": report_date,
            "kapasite_denetim_tarihi": audit_date,
            "ayni_gun": same_day,
        },
        "bolgeler": regions,
        "kapasite_senaryosu_iki_bolgede_mumkun": capacity_ready,
        "onerilen_eylem": recommendation,
        "neden": reason,
    }
    _write_preview_if_meaningful(payload)
    return payload


def _self_check():
    sample_report = {
        "ozet": "Saha sonucu: 2 kontrol (0 şantiye/kazı, 0 yol/altyapı, 1 tarla/bitki, 1 yanlış pozitif)"
    }
    assert _field_counts(sample_report) == (2, 0)

    record = {
        "durum": "ok",
        "ham_olcek_dagilimi": {
            "santiye_olcegi_800_10000": 100,
            "genis_10000_ustu": 20,
        },
        "raporda_olcek_dagilimi": {
            "santiye_olcegi_800_10000": 6,
            "genis_10000_ustu": 9,
        },
    }
    preview = _region_preview(record)
    assert preview["bire_bir_takasla_teorik_ek_kapasite"] == 4
    assert preview["teorik_son_800_10000"] == 10
    assert preview["senaryo_kapasite_acisindan_mumkun"] is True

    blocked = dict(record)
    blocked["raporda_olcek_dagilimi"] = {
        "santiye_olcegi_800_10000": 6,
        "genis_10000_ustu": 2,
    }
    blocked_preview = _region_preview(blocked)
    assert blocked_preview["teorik_son_800_10000"] == 8
    assert blocked_preview["senaryo_kapasite_acisindan_mumkun"] is False

    unchanged_first = {"olusturma": "2026-09-03 22:00 +0300", "karar": "URETIME_DOKUNMA"}
    unchanged_second = {"olusturma": "2026-09-03 23:00 +0300", "karar": "URETIME_DOKUNMA"}
    meaningfully_changed = {"olusturma": "2026-09-03 23:00 +0300", "karar": "MEVCUT_KOTAYI_KORU"}
    assert _semantic_payload(unchanged_first) == _semantic_payload(unchanged_second)
    assert _semantic_payload(unchanged_first) != _semantic_payload(meaningfully_changed)

    assert MAIN_ALARM_MIN_M2 == 250
    assert (MICRO_MIN_M2, MICRO_MAX_M2) == (150, 249)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print(
            "15 Eylül kapasite önizleme öz testi başarılı: 250 m² ana eşik ve 150-249 m² "
            "MİKRO diagnostik kilidi korunuyor; senaryo yalnız bire bir temsil kapasitesini ölçüyor."
        )
        return

    payload = build_preview()
    print(
        "15 Eylül kapasite önizlemesi: "
        f"{payload.get('onerilen_eylem')} · "
        f"saha {payload.get('saha_kaniti', {}).get('kontrol', 0)} kontrol / "
        f"{payload.get('saha_kaniti', {}).get('dogrulanmis_santiye_kazi', 0)} doğrulanmış şantiye-kazı."
    )


if __name__ == "__main__":
    main()
