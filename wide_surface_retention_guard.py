"""Geniş-yüzey diagnostik kayıtlarını aynı raporun tekrar işlenmesinde korur.

Geniş-yüzey katmanları aynı günlük rapor üzerinde birden fazla kez çalışabilir.
Ana koruma her çalışmada `arka_plan_genis_yuzey_hareketleri` alanını güncel
aynı-sahne adaylarıyla yeniden kurduğu için, daha önce tarihsel-backlog katmanı
ile operasyon listesinden ayrılmış kayıtlar ikinci çalışmada sessizce kaybolabilir.
Bu dosya yalnız aynı `rapor_tarihi` içindeki mevcut arka-plan kayıtlarını işlem
öncesi geçici olarak yakalar ve işlem sonrası geri birleştirir.

Algılama, Sentinel eşikleri, alarm/görev üretimi ve SQLite radar hafızası değişmez.
250 m² ana eşik ile 150–249 m² Mikro Şantiye politikası bu katmanın dışındadır.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import wide_surface_background_guard as base


ROOT = Path(__file__).resolve().parent
REPORT_JSON = ROOT / "latest_report.json"
SNAPSHOT_PATH = Path(
    os.environ.get(
        "SANTIYE_WIDE_BACKGROUND_SNAPSHOT",
        "/tmp/santiye-radari-wide-background-snapshot.json",
    )
)


def _dict_rows(value):
    if not isinstance(value, list):
        return []
    return [dict(row) for row in value if isinstance(row, dict)]


def _snapshot_payload(payload):
    if not isinstance(payload, dict):
        return {"rapor_tarihi": "", "adaylar": []}
    return {
        "rapor_tarihi": str(payload.get("rapor_tarihi") or ""),
        "adaylar": _dict_rows(payload.get("arka_plan_genis_yuzey_hareketleri")),
    }


def _merge_same_report(payload, snapshot):
    if not isinstance(payload, dict) or not isinstance(snapshot, dict):
        return payload, False
    report_date = str(payload.get("rapor_tarihi") or "")
    snapshot_date = str(snapshot.get("rapor_tarihi") or "")
    if not report_date or report_date != snapshot_date:
        return payload, False

    current = _dict_rows(payload.get("arka_plan_genis_yuzey_hareketleri"))
    retained = _dict_rows(snapshot.get("adaylar"))
    merged = base._dedupe_candidates(current + retained)
    changed = merged != current

    payload["arka_plan_genis_yuzey_hareketleri"] = merged
    payload["ozet"] = base._summary_after_filter(
        payload.get("ozet"),
        _dict_rows(payload.get("saha_adaylari")),
        merged,
    )
    rule = payload.get("arka_plan_kurali")
    if not isinstance(rule, dict):
        rule = {}
        payload["arka_plan_kurali"] = rule
    rule["tekrar_calistirma_koruması"] = {
        "aktif": True,
        "yalniz_ayni_rapor_tarihi": True,
        "alarm": False,
        "saha_gorevi": False,
        "aciklama": (
            "Aynı günlük rapor yeniden işlendiğinde daha önce arka plana ayrılmış "
            "geniş-yüzey diagnostik kayıtları sessizce düşürülmez."
        ),
    }
    return payload, changed


def _self_check():
    kept = {
        "enlem": 38.25,
        "boylam": 26.42,
        "alan_m2": 120000,
        "genis_kompaktlik": 0.08,
        "kanit_kaynagi": "shape_false_positive_audit_tarihsel_tasima",
    }
    current = {
        "enlem": 38.31,
        "boylam": 26.46,
        "alan_m2": 140000,
        "genis_kompaktlik": 0.07,
        "kanit_kaynagi": "shape_false_positive_audit_yaklasik",
    }
    payload = {
        "rapor_tarihi": "2026-09-03",
        "saha_adaylari": [{"enlem": 38.2, "boylam": 26.3, "alan_m2": 600}],
        "arka_plan_genis_yuzey_hareketleri": [current],
        "ozet": "Test · Aktif saha görevi: 1",
    }
    snapshot = {"rapor_tarihi": "2026-09-03", "adaylar": [kept, current]}
    merged, changed = _merge_same_report(dict(payload), snapshot)
    assert changed is True
    assert len(merged["arka_plan_genis_yuzey_hareketleri"]) == 2
    assert any(
        row.get("kanit_kaynagi") == "shape_false_positive_audit_tarihsel_tasima"
        for row in merged["arka_plan_genis_yuzey_hareketleri"]
    )

    wrong_date = {"rapor_tarihi": "2026-09-02", "adaylar": [kept]}
    untouched, changed = _merge_same_report(dict(payload), wrong_date)
    assert changed is False
    assert len(untouched["arka_plan_genis_yuzey_hareketleri"]) == 1


def _load_report():
    if not REPORT_JSON.exists():
        return None
    try:
        payload = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def snapshot():
    _self_check()
    payload = _load_report()
    snap = _snapshot_payload(payload or {})
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps(snap, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Geniş yüzey tekrar-çalıştırma anlık görüntüsü: "
        f"{len(snap['adaylar'])} arka-plan kaydı."
    )


def restore():
    _self_check()
    payload = _load_report()
    if payload is None or not SNAPSHOT_PATH.exists():
        print("Geniş yüzey tekrar-çalıştırma geri yüklemesi: veri yok, değişiklik yok.")
        return
    try:
        snap = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        print("Geniş yüzey tekrar-çalıştırma anlık görüntüsü okunamadı; değişiklik yok.")
        return

    payload, changed = _merge_same_report(payload, snap)
    if not isinstance(payload, dict):
        return
    backgrounds = _dict_rows(payload.get("arka_plan_genis_yuzey_hareketleri"))
    REPORT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    base.annotate_markdown(payload, backgrounds)
    base.write_review(payload.get("rapor_tarihi"), backgrounds, [], [])
    print(
        "Geniş yüzey tekrar-çalıştırma geri yüklemesi: "
        f"{len(backgrounds)} arka-plan kaydı; "
        + ("önceki kayıtlar korundu." if changed else "ek geri yükleme gerekmedi.")
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["snapshot", "restore"], nargs="?")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Geniş yüzey tekrar-çalıştırma koruması öz testi başarılı.")
        return
    if args.mode == "snapshot":
        snapshot()
    elif args.mode == "restore":
        restore()
    else:
        parser.error("mode gerekli: snapshot veya restore")


if __name__ == "__main__":
    main()
