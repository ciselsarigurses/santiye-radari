"""Günlük saha rotasını Çeşme–Uzunkuyu operasyon odağında tutar.

Kullanıcı yönlendirmesine göre Gülbahçe şu an günlük ekip rotası önceliği değildir.
Bu guard yalnız mahalle etiketi açıkça ``Gülbahçe`` olan aktif adaylarda
``rota_uygun=False`` işaretler; kaydı, alarmı veya saha görevini silmez. Uzunkuyu,
Germiyan, Ildır veya mevkii doğrulanmamış adayları coğrafi tahminle elemez.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REPORT_FILE = Path(__file__).with_name("latest_report.json")
LOW_PRIORITY_NEIGHBORHOODS = {"gülbahçe"}


def _normalize(value: Any) -> str:
    return str(value or "").strip().casefold()


def apply_focus(report: dict[str, Any]) -> dict[str, Any]:
    result = dict(report)
    candidates = []
    background = []
    for raw in report.get("saha_adaylari") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        if _normalize(item.get("mahalle")) in LOW_PRIORITY_NEIGHBORHOODS:
            item["rota_uygun"] = False
            item["operasyon_odak_disinda"] = True
            item["operasyon_odak_notu"] = (
                "Gülbahçe mevcut operasyon odağında günlük ekip rotasına alınmıyor; "
                "kayıt silinmeden arka planda izleniyor."
            )
            background.append(
                {
                    "gorev_id": item.get("gorev_id"),
                    "mahalle": item.get("mahalle"),
                    "enlem": item.get("enlem"),
                    "boylam": item.get("boylam"),
                    "alan_m2": item.get("alan_m2"),
                    "saha_durumu": item.get("saha_durumu"),
                }
            )
        candidates.append(item)
    result["saha_adaylari"] = candidates
    result["operasyon_odak_arka_plan"] = background
    result["operasyon_odak_arka_plan_sayi"] = len(background)
    result["operasyon_odak_notu"] = (
        "Günlük rota Çeşme yarımadası–Uzunkuyu odağındadır. Yalnız açıkça Gülbahçe "
        "etiketli adaylar rota dışı arka planda tutulur; doğrulanmamış mevki coğrafi "
        "tahminle elenmez."
    )
    return result


def _self_check() -> None:
    base = {"gorev_id": "A", "rota_uygun": True, "saha_durumu": "TEKRAR_GIT"}
    report = {
        "saha_adaylari": [
            {**base, "mahalle": "Gülbahçe", "enlem": 38.331547, "boylam": 26.644338},
            {**base, "gorev_id": "B", "mahalle": "Mevki doğrulanmadı", "enlem": 38.32015, "boylam": 26.562138},
            {**base, "gorev_id": "C", "mahalle": "Uzunkuyu", "enlem": 38.32, "boylam": 26.56},
        ]
    }
    result = apply_focus(report)
    rows = {row["gorev_id"]: row for row in result["saha_adaylari"]}
    assert rows["A"]["rota_uygun"] is False
    assert rows["B"]["rota_uygun"] is True
    assert rows["C"]["rota_uygun"] is True
    assert result["operasyon_odak_arka_plan_sayi"] == 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.self_check:
        print("Operasyon odak koruması öz testi başarılı.")
        return

    if not REPORT_FILE.exists():
        raise RuntimeError("latest_report.json bulunamadı.")
    report = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise RuntimeError("latest_report.json nesne olmalıdır.")
    result = apply_focus(report)
    REPORT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Operasyon odak koruması: arka_plan={result['operasyon_odak_arka_plan_sayi']}")


if __name__ == "__main__":
    main()
