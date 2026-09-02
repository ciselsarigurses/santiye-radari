"""Güncel sert kıyı filtresine düşen eski görevleri ilk-3 rotada geri plana iter.

Görevleri kapatmaz, alarm üretmez ve Sentinel eşiğini değiştirmez. Yalnız sahada
kıyı/kayalık mekanizması yanlış pozitif olarak doğrulanmış bir bölgede, son analizde
tekrar görünmeyen ve güncel üretim 30 m SCL-su tamponunun içinde kalan eski açık bir
görev günün ilk üçüne seçilmişse, aynı GECİKEN sınıfındaki güvenli bir alternatifle
değiştirir. TEKRAR_GIT gibi insan kararı hiçbir zaman bastırılmaz.
"""

from __future__ import annotations

import json
from pathlib import Path

from daily_route_shortlist import (
    _actionable_candidates,
    _inject_markdown,
    _rank_for_route,
    _shortlist_markdown,
)


BASE_DIR = Path(__file__).resolve().parent
REPORT_JSON = BASE_DIR / "latest_report.json"
FIELD_REPORT_MD = BASE_DIR / "SAHA_RAPORU.md"
RISK_AUDIT = BASE_DIR / "stale_coastal_task_audit.json"


def _load_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _risk_ids(audit, report_date):
    audit_date = str(audit.get("olusturma") or "")[:10]
    if not report_date or audit_date != str(report_date)[:10]:
        return set()
    values = set()
    for region in (audit.get("bolgeler") or {}).values():
        if not isinstance(region, dict):
            continue
        for item in region.get("gorevler") or []:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("gorev_id") or "").strip()
            if task_id:
                values.add(task_id)
    return values


def _replacement_pool(candidates, selected_ids, risk_ids, priority, preferred_region):
    ranked = _rank_for_route(_actionable_candidates(candidates))
    eligible = [
        item
        for item in ranked
        if str(item.get("gorev_id") or "") not in selected_ids
        and str(item.get("gorev_id") or "") not in risk_ids
        and str(item.get("oncelik") or "").strip().upper() == priority
    ]
    same_region = [
        item for item in eligible
        if str(item.get("bolge") or "") == preferred_region
    ]
    return same_region or eligible


def replace_risky_shortlist(shortlist, candidates, risk_ids):
    selected = [dict(item) for item in (shortlist or []) if isinstance(item, dict)]
    if not selected or not risk_ids:
        return selected, []

    changed = []
    selected_ids = {str(item.get("gorev_id") or "") for item in selected}
    for index, item in enumerate(list(selected)):
        task_id = str(item.get("gorev_id") or "")
        if task_id not in risk_ids:
            continue
        if str(item.get("saha_durumu") or "").strip().upper() == "TEKRAR_GIT":
            continue
        priority = str(item.get("oncelik") or "").strip().upper()
        if priority != "GECİKEN":
            continue

        pool = _replacement_pool(
            candidates,
            selected_ids,
            risk_ids,
            priority,
            str(item.get("bolge") or ""),
        )
        if not pool:
            continue
        replacement = dict(pool[0])
        selected_ids.discard(task_id)
        selected_ids.add(str(replacement.get("gorev_id") or ""))
        selected[index] = replacement
        changed.append(
            {
                "elenen": task_id,
                "yerine": replacement.get("gorev_id"),
            }
        )

    for order, item in enumerate(selected, start=1):
        item["gunluk_sira"] = order
    return selected, changed


def apply_guard():
    payload = _load_json(REPORT_JSON)
    if not payload:
        return []
    report_date = str(payload.get("rapor_tarihi") or "")[:10]
    risks = _risk_ids(_load_json(RISK_AUDIT), report_date)
    if not risks:
        return []

    shortlist = payload.get("gunun_ilk_3_kontrolu") or []
    candidates = payload.get("saha_adaylari") or []
    updated, changes = replace_risky_shortlist(shortlist, candidates, risks)
    if not changes:
        return []

    payload["gunun_ilk_3_kontrolu"] = updated
    payload["gunun_ilk_3_notu"] = (
        str(payload.get("gunun_ilk_3_notu") or "").rstrip()
        + " Güncel sert kıyı filtresine düşen, son analizde tekrar görünmeyen eski "
        "görevler açık kalır ancak eş sınıfta güvenli alternatif varken ilk üç saha "
        "rotasına alınmaz."
    ).strip()
    REPORT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if FIELD_REPORT_MD.exists():
        current = FIELD_REPORT_MD.read_text(encoding="utf-8")
        updated_md = _inject_markdown(current, _shortlist_markdown(updated))
        FIELD_REPORT_MD.write_text(updated_md, encoding="utf-8")
    return changes


def _self_check():
    west = "Çeşme merkez · Alaçatı · Ilıca"
    selected = [
        {
            "gorev_id": "RISK", "saha_durumu": "KONTROLE_GIT", "oncelik": "GECİKEN",
            "bolge": west, "enlem": 38.30, "boylam": 26.30, "alan_m2": 400,
            "boyut_sinifi": "KUCUK", "uydu_onceligi": "ORTA", "bekleme_gun": 5,
        },
        {
            "gorev_id": "KEEP", "saha_durumu": "KONTROLE_GIT", "oncelik": "GECİKEN",
            "bolge": west, "enlem": 38.31, "boylam": 26.31, "alan_m2": 600,
            "boyut_sinifi": "KUCUK", "uydu_onceligi": "ORTA", "bekleme_gun": 4,
        },
    ]
    candidates = selected + [
        {
            "gorev_id": "SAFE", "saha_durumu": "KONTROLE_GIT", "oncelik": "GECİKEN",
            "bolge": west, "enlem": 38.32, "boylam": 26.32, "alan_m2": 500,
            "boyut_sinifi": "KUCUK", "uydu_onceligi": "ORTA", "bekleme_gun": 3,
        }
    ]
    updated, changes = replace_risky_shortlist(selected, candidates, {"RISK"})
    assert [item["gorev_id"] for item in updated] == ["SAFE", "KEEP"], updated
    assert changes == [{"elenen": "RISK", "yerine": "SAFE"}], changes

    human = [dict(selected[0], saha_durumu="TEKRAR_GIT")]
    untouched, changes = replace_risky_shortlist(human, candidates, {"RISK"})
    assert untouched[0]["gorev_id"] == "RISK"
    assert not changes


if __name__ == "__main__":
    _self_check()
    changes = apply_guard()
    if changes:
        print("Kıyı-riski ilk-3 koruması uygulandı: " + json.dumps(changes, ensure_ascii=False))
    else:
        print("İlk üç rotada güncel kıyı filtresine düşen tarihsel görev yok.")
