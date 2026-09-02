"""150-249 m² mikro şantiye zaman-serisini Sentinel yörünge geometrisiyle korur.

Mikro zaman-serisi katmanı aynı 3x3 yamada üç Sentinel sahnesini karşılaştırır.
Aynı MGRS karosunda farklı göreli yörüngeler bulunabildiği için bina/kenar paralaksı,
gölge ve aydınlanma farkı gerçek hafriyat gibi ani başlangıç skoru üretebilir.

Bu koruma üç sahnenin göreli yörüngesi biliniyor ve birbirinden farklıysa yalnız
"ani başlangıç" desteğini diagnostik olarak aşağı sınıflar. Mikro kayıt silinmez;
lokalite, spektral sinyal ve diğer kanıtlarla arka planda izlenmeye devam eder.
Ana 250 m² üretim eşiği, alarm ve saha görevi değiştirilmez.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import satellite


AUDIT_FILE = Path(__file__).with_name("micro_site_temporal_review.json")
SCENE_FIELDS = (
    ("degisim_oncesi_item", "degisim_oncesi"),
    ("onceki_item", "onceki"),
    ("son_item", "son"),
)


def _find_item(items, item_id):
    wanted = str(item_id or "")
    return next((item for item in items if str(item.get("id") or "") == wanted), None)


def _orbit_state(orbits):
    values = list(orbits.values())
    if any(value is None for value in values):
        return "BILINMIYOR"
    return "AYNI" if len(set(values)) == 1 else "FARKLI"


def _apply_state(region_data, orbits):
    state = _orbit_state(orbits)
    region_data["goreli_yorungeler"] = dict(orbits)
    region_data["yorunge_tutarliligi"] = state

    risk = state == "FARKLI"
    for row in region_data.get("adaylar") or []:
        if not isinstance(row, dict):
            continue
        row["yorunge_geometri_riski"] = risk if state != "BILINMIYOR" else None
        if risk and bool(row.get("ani_baslangic_destegi")):
            row["ani_baslangic_destegi"] = False
            row["ani_baslangic_nedeni"] = "FARKLI_GORELI_YORUNGE_GEOMETRISI"
            if row.get("temporal_sinif") == "ANI_BASLANGIC_DESTEGI":
                row["temporal_sinif"] = "YORUNGE_GEOMETRISI_BEKLE"

    region_data["ani_baslangic_destegi"] = sum(
        1
        for row in region_data.get("adaylar") or []
        if isinstance(row, dict) and bool(row.get("ani_baslangic_destegi"))
    )

    if state == "AYNI":
        region_data["yorunge_notu"] = (
            "Üç Sentinel sahnesi aynı göreli yörüngede; mikro ani başlangıç kanıtı "
            "bakış geometrisi açısından destekleniyor."
        )
    elif state == "FARKLI":
        region_data["yorunge_notu"] = (
            "Üç Sentinel sahnesi aynı göreli yörüngede değil; paralaks/gölge ve "
            "aydınlanma farkını yeni hafriyat sanmamak için ani başlangıç desteği "
            "diagnostik olarak beklemeye alındı. Kayıt silinmedi."
        )
    else:
        region_data["yorunge_notu"] = (
            "En az bir Sentinel sahnesinde göreli yörünge bilgisi çözülemedi; mevcut "
            "mikro temporal etiketi değiştirilmedi."
        )
    return state


def guard_payload(payload):
    if not isinstance(payload, dict):
        return payload

    regions = payload.get("bolgeler") or {}
    if not isinstance(regions, dict):
        return payload

    for region_key, region_data in regions.items():
        if region_key not in satellite.REGIONS:
            continue
        if not isinstance(region_data, dict) or region_data.get("durum") != "ok":
            continue
        if not region_data.get("degisim_oncesi_item"):
            continue

        try:
            items = satellite._search_items(satellite.REGIONS[region_key]["bbox"])
            orbits = {}
            for field, label in SCENE_FIELDS:
                item = _find_item(items, region_data.get(field))
                orbits[label] = satellite._relative_orbit(item) if item else None
            _apply_state(region_data, orbits)
        except Exception as exc:
            # Yanlış-pozitif koruması STAC geçici hatası yüzünden zinciri kırmasın.
            region_data["yorunge_tutarliligi"] = "KONTROL_EDILEMEDI"
            region_data["yorunge_notu"] = f"Göreli yörünge kontrolü yapılamadı: {exc}"

    total = payload.get("toplam")
    if isinstance(total, dict):
        total["ani_baslangic_destegi"] = sum(
            int(data.get("ani_baslangic_destegi") or 0)
            for data in regions.values()
            if isinstance(data, dict)
        )
    return payload


def _self_check():
    assert satellite.MIN_HOTSPOT_AREA_M2 == 250

    region = {
        "adaylar": [
            {"ani_baslangic_destegi": True, "temporal_sinif": "ANI_BASLANGIC_DESTEGI"},
            {"ani_baslangic_destegi": False, "temporal_sinif": "TEK_DONEM_SINYALI"},
        ]
    }
    state = _apply_state(region, {"degisim_oncesi": 36, "onceki": 36, "son": 36})
    assert state == "AYNI"
    assert region["adaylar"][0]["ani_baslangic_destegi"] is True
    assert region["adaylar"][0]["yorunge_geometri_riski"] is False

    region = {
        "adaylar": [
            {"ani_baslangic_destegi": True, "temporal_sinif": "ANI_BASLANGIC_DESTEGI"}
        ]
    }
    state = _apply_state(region, {"degisim_oncesi": 36, "onceki": 79, "son": 36})
    assert state == "FARKLI"
    assert region["adaylar"][0]["ani_baslangic_destegi"] is False
    assert region["adaylar"][0]["temporal_sinif"] == "YORUNGE_GEOMETRISI_BEKLE"
    assert region["adaylar"][0]["yorunge_geometri_riski"] is True
    assert region["ani_baslangic_destegi"] == 0

    region = {
        "adaylar": [
            {"ani_baslangic_destegi": True, "temporal_sinif": "ANI_BASLANGIC_DESTEGI"}
        ]
    }
    state = _apply_state(region, {"degisim_oncesi": None, "onceki": 36, "son": 36})
    assert state == "BILINMIYOR"
    assert region["adaylar"][0]["ani_baslangic_destegi"] is True
    assert region["adaylar"][0]["yorunge_geometri_riski"] is None


def run_guard():
    _self_check()
    if not AUDIT_FILE.exists():
        raise RuntimeError("micro_site_temporal_review.json bulunamadı.")
    payload = json.loads(AUDIT_FILE.read_text(encoding="utf-8"))
    guarded = guard_payload(payload)
    AUDIT_FILE.write_text(
        json.dumps(guarded, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return guarded


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Mikro Sentinel yörünge koruması öz testi başarılı; alarm/görev/eşik değişmedi.")
        return

    payload = run_guard()
    parts = []
    for region_key, data in (payload.get("bolgeler") or {}).items():
        if not isinstance(data, dict) or data.get("durum") != "ok":
            continue
        parts.append(
            f"{region_key}={data.get('yorunge_tutarliligi')} "
            f"(ani={int(data.get('ani_baslangic_destegi') or 0)})"
        )
    print(
        "Mikro Sentinel yörünge koruması tamamlandı: "
        + (", ".join(parts) or "uygun bölge yok")
        + ". Kayıtlar silinmedi; üretim alarmı/görev değişmedi."
    )


if __name__ == "__main__":
    main()
