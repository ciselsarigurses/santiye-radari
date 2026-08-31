"""Alarm dışı saha kontrol katmanlarının aynı mahalleyi gereksiz tekrar etmesini azaltır.

Günlük rota kısa listesi kuru-zemin diagnostiklerinden en fazla iki kalibrasyon noktası,
Sentinel kör alan denetimi de en fazla iki kapsama noktası gösterebilir. Her iki katman
ayrı ayrı güvenli olsa da aynı gün aynı yaklaşık mahalleleri seçmeleri yarımada kapsamasını
daraltır. Bu post-process katmanı alarm/görev üretmez, toplam nokta sayısını artırmaz ve
mevcut kör-alan seçimindeki mesafe/alan güvenliklerini değiştirmez. Yalnız güvenli bir
alternatif varsa kör-alan havuzunda, kuru-zemin kalibrasyonunda zaten temsil edilen
mahalleleri ikinci kez seçmemeyi tercih eder.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import coverage_patrol_shortlist as coverage


AUDIT_JSON = Path(__file__).with_name("coverage_blind_area_audit.json")
REPORT_JSON = Path(__file__).with_name("latest_report.json")
FIELD_REPORT_MD = Path(__file__).with_name("SAHA_RAPORU.md")
NOTE = (
    "Alarm/görev değildir; tarihsel Sentinel verisinde kara olduğu doğrulanmış kalıcı "
    "gözlem boşluklarından günlük rotasyonla en fazla iki insan kontrol noktası seçilir. "
    "Aktif radar görevlerinden >=150 m, birbirinden >=250 m uzakta tutulur. Kuru zemin "
    "kalibrasyonunda aynı gün zaten temsil edilen mahalleler, güvenli alternatif varsa "
    "kör alan devriyesinde tekrar seçilmez."
)


def _neighborhoods_from_calibration(report_payload):
    values = set()
    for item in (report_payload or {}).get("kuru_zemin_kalibrasyon_kontrolu") or []:
        if not isinstance(item, dict):
            continue
        key = coverage._neighborhood_key(item.get("mahalle"))
        if key:
            values.add(key)
    return values


def _prefer_other_neighborhoods(audit_payload, blocked_neighborhoods):
    """Her bölgede güvenli alternatif varsa kalibrasyon mahallesini havuzdan çıkar."""
    if not blocked_neighborhoods:
        return audit_payload

    adjusted = copy.deepcopy(audit_payload)
    regions = adjusted.get("bolgeler") or {}
    if not isinstance(regions, dict):
        return adjusted

    for region_data in regions.values():
        if not isinstance(region_data, dict):
            continue
        for field in ("kor_alan_devriye_ornekleri", "ornekler"):
            examples = region_data.get(field)
            if not isinstance(examples, list) or not examples:
                continue
            alternatives = [
                item
                for item in examples
                if isinstance(item, dict)
                and coverage._neighborhood_key(item.get("mahalle_yaklasik"))
                not in blocked_neighborhoods
            ]
            # Tercih yalnız alternatif gerçekten varsa uygulanır; aksi halde bölgeyi
            # kör bırakmamak için orijinal havuz korunur.
            if alternatives:
                region_data[field] = alternatives
    return adjusted


def select_diverse_coverage_patrol(audit_payload, report_payload, limit=coverage.TOTAL_LIMIT):
    blocked = _neighborhoods_from_calibration(report_payload)
    adjusted = _prefer_other_neighborhoods(audit_payload, blocked)
    return coverage.select_coverage_patrol(adjusted, report_payload, limit=limit)


def _markdown(items):
    section = coverage._markdown(items)
    old = (
        "Aynı görüntü günlerce değişmezse noktalar günlük rotasyonla değişir."
    )
    new = (
        old
        + " Kuru zemin kalibrasyonunda aynı gün zaten temsil edilen mahalleler, "
        "güvenli alternatif varsa burada tekrar seçilmez."
    )
    return section.replace(old, new, 1)


def update_route_diversity():
    if not AUDIT_JSON.exists() or not REPORT_JSON.exists():
        return []

    audit = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
    report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    selected = select_diverse_coverage_patrol(audit, report)

    report["kor_alan_saha_devriyesi"] = selected
    report["kor_alan_saha_devriyesi_notu"] = NOTE
    REPORT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if FIELD_REPORT_MD.exists():
        current = FIELD_REPORT_MD.read_text(encoding="utf-8")
        FIELD_REPORT_MD.write_text(
            coverage._inject(current, _markdown(selected)),
            encoding="utf-8",
        )
    return selected


def _self_check():
    coverage._self_check()
    audit = {
        "bolgeler": {
            "west": {
                "durum": "ok",
                "bolge": "Batı",
                "kara_referans_sahne_sayisi": 8,
                "kor_alan_devriye_ornekleri": [
                    {
                        "mahalle_yaklasik": "Dalyan",
                        "enlem": 38.35,
                        "boylam": 26.30,
                        "alan_m2": 400,
                        "neden": "BULUT_GOLGE_KALICI",
                    },
                    {
                        "mahalle_yaklasik": "Ovacık",
                        "enlem": 38.25,
                        "boylam": 26.33,
                        "alan_m2": 500,
                        "neden": "BULUT_GOLGE_KALICI",
                    },
                ],
            },
            "east": {
                "durum": "ok",
                "bolge": "Doğu",
                "kara_referans_sahne_sayisi": 8,
                "kor_alan_devriye_ornekleri": [
                    {
                        "mahalle_yaklasik": "Ildır",
                        "enlem": 38.40,
                        "boylam": 26.48,
                        "alan_m2": 400,
                        "neden": "BULUT_GOLGE_KALICI",
                    },
                    {
                        "mahalle_yaklasik": "Germiyan",
                        "enlem": 38.32,
                        "boylam": 26.50,
                        "alan_m2": 600,
                        "neden": "KARISIK_GECERSIZLIK",
                    },
                ],
            },
        }
    }
    report = {
        "rapor_tarihi": "2026-08-31",
        "saha_adaylari": [],
        "kuru_zemin_kalibrasyon_kontrolu": [
            {"mahalle": "Dalyan", "enlem": 38.36, "boylam": 26.31},
            {"mahalle": "Ildır", "enlem": 38.40, "boylam": 26.62},
        ],
    }
    selected = select_diverse_coverage_patrol(audit, report)
    assert {item["mahalle"] for item in selected} == {"Ovacık", "Germiyan"}, selected
    assert all(item["alarm"] is False for item in selected)
    assert len(selected) == coverage.TOTAL_LIMIT

    fallback_audit = {
        "bolgeler": {
            "only": {
                "durum": "ok",
                "bolge": "Tek",
                "kara_referans_sahne_sayisi": 8,
                "kor_alan_devriye_ornekleri": [
                    {
                        "mahalle_yaklasik": "Dalyan",
                        "enlem": 38.35,
                        "boylam": 26.30,
                        "alan_m2": 400,
                        "neden": "BULUT_GOLGE_KALICI",
                    }
                ],
            }
        }
    }
    fallback = select_diverse_coverage_patrol(fallback_audit, report, limit=1)
    assert fallback and fallback[0]["mahalle"] == "Dalyan"


if __name__ == "__main__":
    _self_check()
    chosen = update_route_diversity()
    print(
        "Saha devriye çeşitliliği güncellendi: "
        + (", ".join(str(item.get("mahalle") or "?") for item in chosen) or "ek nokta yok")
    )
