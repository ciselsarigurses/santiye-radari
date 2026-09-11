"""Manuel MİKRO saha sonuçlarını merkezi rapor kalibrasyonuna güvenli biçimde bağlar.

Bu köprü 150-249 m² MİKRO saha geri bildirimini yalnız sonuç sayımı/kalibrasyon
metadata'sına dahil eder. Yeni alarm veya saha görevi üretmez, 250 m² ana eşiğini
ve MİKRO seçim politikasını değiştirmez. GPS/EXIF ile bağımsız doğrulanmamış bir
manuel eşleşme şantiye sınıfı kalibrasyonuna girebilir; koordinat hassasiyeti
başarısı olarak sayılmaz.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DB_FILE = ROOT / "santiye.db"
REPORT_FILE = ROOT / "latest_report.json"
FIELD_REPORT_FILE = ROOT / "SAHA_RAPORU.md"
MANUAL_FILE = ROOT / "micro_pending_field_checks.json"

OUTCOME_KEYS = (
    "SANTIYE_KAZI",
    "YIKIM_TEMIZLIK",
    "YOL_ALTYAPI",
    "TARLA_BITKI",
    "YANLIS_POZITIF",
)
RESOLVED_STATUS = "SAHA_KONTROLU_DOGRULANDI"
SUMMARY_PATTERN = re.compile(r" · Saha sonucu: \d+ kontrol \([^)]*\)")


def _empty_counts():
    return {key: 0 for key in OUTCOME_KEYS}


def _db_outcomes(db_path=DB_FILE):
    counts = _empty_counts()
    task_ids = set()
    if not Path(db_path).exists():
        return counts, task_ids
    try:
        connection = sqlite3.connect(str(db_path))
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='saha_sonuclari'"
            ).fetchone()
            if not table:
                return counts, task_ids
            rows = connection.execute("SELECT gorev_id,sonuc FROM saha_sonuclari").fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return counts, task_ids

    for task_id, outcome in rows:
        task_id = str(task_id or "").strip().upper()
        outcome = str(outcome or "").strip().upper()
        if task_id:
            task_ids.add(task_id)
        if outcome in counts:
            counts[outcome] += 1
    return counts, task_ids


def _manual_outcomes(payload, db_task_ids=None):
    counts = _empty_counts()
    accepted = []
    seen = set()
    db_task_ids = {str(value).strip().upper() for value in (db_task_ids or set())}

    if not isinstance(payload, dict):
        return counts, accepted
    # Bu dosyanın görev/alarm kaynağı olmadığını veri sözleşmesi düzeyinde koru.
    if payload.get("alarm") is not False or payload.get("saha_gorevi") is not False:
        return counts, accepted

    for raw in payload.get("kayitlar") or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("durum") or "").strip().upper() != RESOLVED_STATUS:
            continue
        outcome = str(raw.get("sonuc") or "").strip().upper()
        if outcome not in counts:
            continue
        record_id = str(raw.get("kayit_id") or "").strip().upper()
        if not record_id or record_id in seen:
            continue

        aliases = {
            str(raw.get(key) or "").strip().upper()
            for key in ("kayit_id", "gorev_id", "ana_gorev_id", "saha_gorev_id")
            if str(raw.get(key) or "").strip()
        }
        # Aynı sonuç daha sonra ana saha tablosuna aynı kimlikle aktarılırsa iki kez sayma.
        if aliases & db_task_ids:
            seen.add(record_id)
            continue

        seen.add(record_id)
        counts[outcome] += 1
        accepted.append(dict(raw))
    return counts, accepted


def _combined_counts(db_counts, manual_counts):
    return {
        key: int(db_counts.get(key, 0) or 0) + int(manual_counts.get(key, 0) or 0)
        for key in OUTCOME_KEYS
    }


def _summary_with_outcomes(summary, counts):
    summary = SUMMARY_PATTERN.sub("", str(summary or "")).rstrip()
    total = sum(int(counts.get(key, 0) or 0) for key in OUTCOME_KEYS)
    if not total:
        return summary
    return summary + (
        f" · Saha sonucu: {total} kontrol "
        f"({counts['SANTIYE_KAZI']} şantiye/kazı, "
        f"{counts['YIKIM_TEMIZLIK']} yıkım/temizlik öncülü, "
        f"{counts['YOL_ALTYAPI']} yol/altyapı, "
        f"{counts['TARLA_BITKI']} tarla/bitki, "
        f"{counts['YANLIS_POZITIF']} yanlış pozitif)"
    )


def _manual_metadata(manual_counts, accepted):
    independent_location = sum(
        item.get("saha_konumu_bagimsiz_dogrulandi") is True for item in accepted
    )
    return {
        "toplam": sum(manual_counts.values()),
        "sonuclar": manual_counts,
        "bagimsiz_konum_dogrulanan": independent_location,
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "not": (
            "Manuel MİKRO saha sonucu sınıf kalibrasyonuna dahil edilir. "
            "Yalnız açıkça bağımsız doğrulandı alanı true olan kayıt koordinat "
            "hassasiyeti doğrulaması sayılır; GPS/EXIF yokluğu bu sayıyı artırmaz."
        ),
    }


def _patch_markdown(text, summary):
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("**Özet:**"):
            lines[index] = f"**Özet:** {summary}"
            return "\n".join(lines) + ("\n" if str(text or "").endswith("\n") else "")
    return text


def apply_bridge():
    if not REPORT_FILE.exists() or not MANUAL_FILE.exists():
        return False, None
    try:
        report = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
        manual = json.loads(MANUAL_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, None
    if not isinstance(report, dict):
        return False, None

    db_counts, db_task_ids = _db_outcomes()
    manual_counts, accepted = _manual_outcomes(manual, db_task_ids)
    counts = _combined_counts(db_counts, manual_counts)
    summary = _summary_with_outcomes(report.get("ozet"), counts)

    updated = dict(report)
    updated["ozet"] = summary
    updated["manuel_mikro_saha_kalibrasyonu"] = _manual_metadata(manual_counts, accepted)
    before = REPORT_FILE.read_text(encoding="utf-8")
    after = json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
    changed = before != after
    if changed:
        REPORT_FILE.write_text(after, encoding="utf-8")

    if FIELD_REPORT_FILE.exists():
        md_before = FIELD_REPORT_FILE.read_text(encoding="utf-8")
        md_after = _patch_markdown(md_before, summary)
        if md_after != md_before:
            FIELD_REPORT_FILE.write_text(md_after, encoding="utf-8")
            changed = True

    return changed, {
        "toplam": sum(counts.values()),
        "sonuclar": counts,
        "manuel_mikro": sum(manual_counts.values()),
        "manuel_bagimsiz_konum": sum(
            item.get("saha_konumu_bagimsiz_dogrulandi") is True for item in accepted
        ),
    }


def _self_check():
    db_counts = _empty_counts()
    db_counts.update({"YIKIM_TEMIZLIK": 1, "TARLA_BITKI": 1, "YANLIS_POZITIF": 1})
    manual = {
        "alarm": False,
        "saha_gorevi": False,
        "kayitlar": [
            {
                "kayit_id": "MM-1",
                "durum": RESOLVED_STATUS,
                "sonuc": "SANTIYE_KAZI",
                "konum_dogrulama": "KULLANICI_ADAY_ESLEMESI_GPS_EXIF_YOK",
            },
            {
                "kayit_id": "MM-1",
                "durum": RESOLVED_STATUS,
                "sonuc": "SANTIYE_KAZI",
            },
        ],
    }
    manual_counts, accepted = _manual_outcomes(manual, set())
    combined = _combined_counts(db_counts, manual_counts)
    assert sum(combined.values()) == 4
    assert combined["SANTIYE_KAZI"] == 1
    assert len(accepted) == 1
    assert _manual_metadata(manual_counts, accepted)["bagimsiz_konum_dogrulanan"] == 0

    duplicate_in_db, accepted_in_db = _manual_outcomes(manual, {"MM-1"})
    assert sum(duplicate_in_db.values()) == 0 and not accepted_in_db

    old = (
        "İnternet: 0 yeni. · Saha sonucu: 3 kontrol "
        "(0 şantiye/kazı, 1 yıkım/temizlik öncülü, 0 yol/altyapı, "
        "1 tarla/bitki, 1 yanlış pozitif)"
    )
    new = _summary_with_outcomes(old, combined)
    assert "Saha sonucu: 4 kontrol (1 şantiye/kazı" in new
    assert new.count("Saha sonucu:") == 1

    md = "# Rapor\n\n**Özet:** eski\n\nDevam\n"
    assert "**Özet:** " + new in _patch_markdown(md, new)
    assert _manual_metadata(manual_counts, accepted)["ana_uretim_esigi_m2"] == 250
    assert _manual_metadata(manual_counts, accepted)["mikro_aralik_m2"] == [150, 249]
    print("OK: manuel MİKRO saha sonucu merkezi kalibrasyona tekil ve koordinat-güvenli bağlanıyor.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        _self_check()
        return
    changed, result = apply_bridge()
    if result is None:
        print("Manuel MİKRO saha köprüsü: gerekli rapor/veri bulunamadı, değişiklik yok.")
        return
    print(
        "Manuel MİKRO saha köprüsü: "
        f"toplam={result['toplam']}, şantiye/kazı={result['sonuclar']['SANTIYE_KAZI']}, "
        f"manuel_mikro={result['manuel_mikro']}, "
        f"bağımsız_konum={result['manuel_bagimsiz_konum']}, değişti={changed}."
    )


if __name__ == "__main__":
    main()
