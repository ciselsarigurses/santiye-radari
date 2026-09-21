"""Saha Kontrol ekranı için güvenli uydu görev kaynağını oluşturur.

Normal ``saha_adaylari`` havuzu diagnostik/backlog kayıtlarını korur. Temel/kepçe
kapısı aktif olduğunda bazı yüksek güvenli, çoklu-kanıt adayları doğrudan nihai
``gunun_ilk_3_kontrolu`` rotasına enjekte edilebilir ve ham aday havuzunda yer
almayabilir. Bu yardımcı, o rota-only kayıtları Saha Kontrol kaynağına ekler;
mevcut açık görev kimliklerini tekilleştirir. Hangi kayıtların eyleme açık
olduğuna dair nihai whitelist yine sayfadaki temel/kepçe kapısı filtresidir.
"""

from __future__ import annotations

import json
from pathlib import Path


BASE = Path(__file__).resolve().parent
REPORT_JSON = BASE / "latest_report.json"


def _task_id(item: dict) -> str:
    """Yalnız açıkça taşınan görev kimliğini kullan; burada yeni kimlik üretme."""
    return str(item.get("gorev_id") or "").strip()


def operational_satellite_task_ids(report: dict) -> set[str] | None:
    """Kapı aktifse nihai uydu rotasındaki açık görev kimliklerini döndür."""
    if report.get("temel_kazi_kapisi_aktif") is not True:
        return None

    task_ids: set[str] = set()
    for raw in report.get("gunun_ilk_3_kontrolu") or []:
        if not isinstance(raw, dict):
            continue
        task_id = _task_id(raw)
        if task_id:
            task_ids.add(task_id)
    return task_ids


def merged_satellite_candidates(report: dict) -> list[dict]:
    """Ham havuz + rota-only adayları açık görev kimliğiyle tekilleştirerek birleştir.

    Temel/kepçe kapısı kapalı eski raporlarda davranış değişmez: yalnız
    ``saha_adaylari`` döner. Kapı aktifse nihai rotada olup ham havuzda olmayan,
    açık ``gorev_id`` taşıyan kayıt eklenir. Bu fonksiyon yeni görev kimliği
    üretmez, görev durumu açmaz ve dış bağımlılık yüklemez.
    """
    merged: list[dict] = []
    seen: set[str] = set()

    for raw in report.get("saha_adaylari") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        task_id = _task_id(item)
        if task_id:
            seen.add(task_id)
        merged.append(item)

    if report.get("temel_kazi_kapisi_aktif") is not True:
        return merged

    for raw in report.get("gunun_ilk_3_kontrolu") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        task_id = _task_id(item)
        if not task_id or task_id in seen:
            continue
        merged.append(item)
        seen.add(task_id)

    return merged


def _self_check() -> None:
    report = {
        "temel_kazi_kapisi_aktif": True,
        "saha_adaylari": [
            {
                "gorev_id": "OLD-BSI",
                "saha_durumu": "KONTROLE_GIT",
                "enlem": 38.31,
                "boylam": 26.31,
            }
        ],
        "gunun_ilk_3_kontrolu": [
            {
                "gorev_id": "FK-ROUTE-ONLY",
                "saha_durumu": "KONTROLE_GIT",
                "enlem": 38.307849,
                "boylam": 26.333503,
                "temel_kazi_coklu_kanit_destekli": True,
            }
        ],
    }
    merged = merged_satellite_candidates(report)
    assert [row.get("gorev_id") for row in merged] == ["OLD-BSI", "FK-ROUTE-ONLY"], merged
    assert operational_satellite_task_ids(report) == {"FK-ROUTE-ONLY"}

    duplicate = dict(report)
    duplicate["saha_adaylari"] = report["saha_adaylari"] + [
        dict(report["gunun_ilk_3_kontrolu"][0])
    ]
    merged = merged_satellite_candidates(duplicate)
    assert [row.get("gorev_id") for row in merged].count("FK-ROUTE-ONLY") == 1, merged

    legacy = {
        "temel_kazi_kapisi_aktif": False,
        "saha_adaylari": [dict(report["saha_adaylari"][0])],
        "gunun_ilk_3_kontrolu": [dict(report["gunun_ilk_3_kontrolu"][0])],
    }
    assert [row.get("gorev_id") for row in merged_satellite_candidates(legacy)] == ["OLD-BSI"]
    assert operational_satellite_task_ids(legacy) is None

    # Kimliği olmayan rota kaydı burada yeni görev kimliğine dönüştürülmez.
    no_id = dict(report)
    no_id["gunun_ilk_3_kontrolu"] = [{"enlem": 38.30, "boylam": 26.33}]
    assert [row.get("gorev_id") for row in merged_satellite_candidates(no_id)] == ["OLD-BSI"]


def _current_report_check() -> None:
    """Mevcut rapordaki nihai rota kimliklerinin kontrol kaynağında kaybolmadığını doğrula."""
    if not REPORT_JSON.exists():
        return
    try:
        report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(report, dict):
        return

    operational = operational_satellite_task_ids(report)
    if operational is None:
        return
    merged_ids = {
        _task_id(row)
        for row in merged_satellite_candidates(report)
        if isinstance(row, dict) and _task_id(row)
    }
    missing = operational - merged_ids
    assert not missing, f"Nihai uydu rota görevi Saha Kontrol kaynağında kayıp: {sorted(missing)}"


if __name__ == "__main__":
    _self_check()
    _current_report_check()
    print("field satellite source self-check: ok")
