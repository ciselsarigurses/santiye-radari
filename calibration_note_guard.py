"""Saha raporundaki kalibrasyon açıklamasını gerçek temporal rotasyonla eşleştirir.

Seçim mantığını değiştirmez. `final_calibration_diversity_guard.py` son kalibrasyon
noktasını yazarken temel açıklamayı yeniden üretir; temporal ani-başlangıç adayları artık
günlük döndüğü için eski "rotasyonun önüne alınır" ifadesinin kullanıcıyı yanıltmaması
amacıyla yalnız açıklama metnini günceller.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import calibration_rotation_guard as calibration


OLD_SENTENCE = (
    "Aynı rapor günü ve aynı Sentinel çifti için zaman-serisi denetimi değişim öncesi "
    "sakin olup sonradan birden güçlenen güvenli bir örnek bulursa o örnek saha teyidi "
    "için rotasyonun önüne alınır; bu yine alarm değildir."
)
NEW_SENTENCE = (
    "Aynı rapor günü ve aynı Sentinel çifti için zaman-serisi denetimi değişim öncesi "
    "sakin olup sonradan birden güçlenen güvenli örnekleri en güçlü dört mahalle-çeşitli "
    "temporal havuzda günlük döndürür. 250-900 m² adayda güvenilir yaygın çevre değişimi "
    "görülürse aday temporal acil havuzdan çıkarılır, normal güvenli rotasyonda korunur; "
    "bu yine alarm değildir."
)
REPORT_APPEND = (
    " Temporal ani-başlangıç desteği olan güvenli adaylar aynı Sentinel sahnesinde en "
    "güçlü dört mahalle-çeşitli aday arasında günlük döner; 250-900 m² adayda güvenilir "
    "yaygın çevre değişimi varsa temporal acil havuzdan çıkarılır ve normal güvenli "
    "rotasyonda kalır."
)


def _updated_markdown(text):
    if OLD_SENTENCE not in text:
        return text
    return text.replace(OLD_SENTENCE, NEW_SENTENCE, 1)


def _updated_note(note):
    base = str(note or "").strip()
    if "Temporal ani-başlangıç desteği olan güvenli adaylar" in base:
        return base
    return base + REPORT_APPEND


def update_note():
    if not calibration.REPORT_JSON.exists():
        return False
    try:
        report = json.loads(calibration.REPORT_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    marker = report.get("temporal_kalibrasyon_rotasyonu") or {}
    if not isinstance(marker, dict) or marker.get("durum") != "uygulandi":
        return False

    report["kuru_zemin_kalibrasyon_notu"] = _updated_note(
        report.get("kuru_zemin_kalibrasyon_notu")
    )
    calibration.REPORT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if calibration.FIELD_REPORT_MD.exists():
        current = calibration.FIELD_REPORT_MD.read_text(encoding="utf-8")
        calibration.FIELD_REPORT_MD.write_text(
            _updated_markdown(current),
            encoding="utf-8",
        )
    return True


def _self_check():
    sample = "başlangıç " + OLD_SENTENCE + " bitiş"
    updated = _updated_markdown(sample)
    assert OLD_SENTENCE not in updated
    assert NEW_SENTENCE in updated

    note = "Alarm/görev değildir."
    first = _updated_note(note)
    second = _updated_note(first)
    assert first == second
    assert "Temporal ani-başlangıç desteği" in first


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Kalibrasyon açıklama tutarlılığı öz testi başarılı.")
        return 0
    changed = update_note()
    print(
        "Kalibrasyon açıklaması temporal rotasyonla eşleştirildi."
        if changed
        else "Temporal rotasyon işareti yok; açıklama değiştirilmedi."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
