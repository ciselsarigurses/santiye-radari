"""Saha Kontrol ekranı için güvenli uydu görev kaynağını oluşturur.

Normal ``saha_adaylari`` havuzu diagnostik/backlog kayıtlarını korur. Temel/kepçe
kapısı aktif olduğunda bazı yüksek güvenli, çoklu-kanıt adayları doğrudan nihai
``gunun_ilk_3_kontrolu`` rotasına enjekte edilebilir ve ham aday havuzunda yer
almayabilir. Bu yardımcı, o rota-only kayıtları Saha Kontrol kaynağına ekler;
mevcut kayıtları görev kimliğiyle tekilleştirir. Hangi kayıtların eyleme açık
olduğuna dair nihai whitelist yine sayfadaki temel/kepçe kapısı filtresidir.
"""

from __future__ import annotations

from field_state import satellite_task_id


def _task_id(item: dict) -> str:
    try:
        return str(item.get("gorev_id") or satellite_task_id(item))
    except (TypeError, ValueError):
        return ""


def operational_satellite_task_ids(report: dict) -> set[str] | None:
    """Kapı aktifse nihai uydu rotasındaki görev kimliklerini döndür."""
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
    """Ham havuz + rota-only adayları görev kimliğiyle tekilleştirerek birleştir.

    Temel/kepçe kapısı kapalı eski raporlarda davranış değişmez: yalnız
    ``saha_adaylari`` döner. Kapı aktifse nihai rotada olup ham havuzda olmayan
    kayıt eklenir. Bu fonksiyon kendi başına yeni görev üretmez veya durum açmaz.
    """
    merged: list[dict] = []
    seen: set[str] = set()

    for raw in report.get("saha_adaylari") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        task_id = _task_id(item)
        if task_id:
            item["gorev_id"] = task_id
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
        item["gorev_id"] = task_id
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


if __name__ == "__main__":
    _self_check()
    print("field satellite source self-check: ok")
