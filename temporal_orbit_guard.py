"""Kuru-zemin zaman serisi kanıtını Sentinel göreli yörünge tutarlılığıyla korur.

`dry_ground_temporal_audit.py` üç Sentinel sahnesindeki aynı 3x3 yamayı karşılaştırır.
Ana uydu seçimi mümkün olduğunda aynı göreli yörüngeyi tercih eder; ancak uygun sahne
yoksa kör kalmamak için aynı MGRS karosundaki farklı yörüngeye güvenli geri dönüş yapar.
Farklı bakış geometrileri bina/kenar paralaksı ve aydınlanma farkı üretebildiğinden,
bu diagnostik koruma üç sahnenin göreli yörüngesi biliniyor ve birbirinden farklıysa
"ani başlangıç" etiketini saha önceliğinde kullanılamaz hale getirir.

Bu dosya üretim alarmı, görev, Sentinel eşiği veya 250 m² alt sınırını değiştirmez.
Yörünge bilgisi eksikse kanıtı silmez; yalnız durumu bilinmiyor olarak kaydeder.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import satellite


AUDIT_FILE = Path(__file__).with_name("dry_ground_temporal_audit.json")
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

    region_data["ani_baslangic_destegi"] = sum(
        1
        for row in region_data.get("adaylar") or []
        if isinstance(row, dict) and bool(row.get("ani_baslangic_destegi"))
    )
    if state == "AYNI":
        region_data["yorunge_notu"] = (
            "Üç Sentinel sahnesi aynı göreli yörüngede; zaman-serisi ani başlangıç "
            "kanıtı bakış geometrisi açısından destekleniyor."
        )
    elif state == "FARKLI":
        region_data["yorunge_notu"] = (
            "Üç Sentinel sahnesi aynı göreli yörüngede değil; farklı bakış geometrisinin "
            "paralaks/aydınlanma etkisini yeni hafriyat sanmamak için ani başlangıç "
            "etiketi yalnız diagnostik olarak aşağı sınıflandı."
        )
    else:
        region_data["yorunge_notu"] = (
            "En az bir Sentinel sahnesinde göreli yörünge bilgisi çözülemedi; mevcut "
            "zaman-serisi etiketi değiştirilmedi."
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

        try:
            items = satellite._search_items(satellite.REGIONS[region_key]["bbox"])
            orbits = {}
            for field, label in SCENE_FIELDS:
                item = _find_item(items, region_data.get(field))
                orbits[label] = satellite._relative_orbit(item) if item else None
            _apply_state(region_data, orbits)
        except Exception as exc:
            # Bu katman yalnız yanlış-pozitif korumasıdır; STAC geçici hatası yüzünden
            # mevcut zaman-serisi raporunu veya günlük radar zincirini kırma.
            region_data["yorunge_tutarliligi"] = "KONTROL_EDILEMEDI"
            region_data["yorunge_notu"] = f"Göreli yörünge kontrolü yapılamadı: {exc}"

    return payload


def _self_check():
    region = {
        "adaylar": [
            {"ani_baslangic_destegi": True},
            {"ani_baslangic_destegi": False},
        ]
    }
    state = _apply_state(region, {"degisim_oncesi": 36, "onceki": 36, "son": 36})
    assert state == "AYNI"
    assert region["adaylar"][0]["ani_baslangic_destegi"] is True
    assert region["adaylar"][0]["yorunge_geometri_riski"] is False

    region = {"adaylar": [{"ani_baslangic_destegi": True}]}
    state = _apply_state(region, {"degisim_oncesi": 36, "onceki": 36, "son": 79})
    assert state == "FARKLI"
    assert region["adaylar"][0]["ani_baslangic_destegi"] is False
    assert region["adaylar"][0]["yorunge_geometri_riski"] is True
    assert region["ani_baslangic_destegi"] == 0

    region = {"adaylar": [{"ani_baslangic_destegi": True}]}
    state = _apply_state(region, {"degisim_oncesi": None, "onceki": 36, "son": 36})
    assert state == "BILINMIYOR"
    assert region["adaylar"][0]["ani_baslangic_destegi"] is True
    assert region["adaylar"][0]["yorunge_geometri_riski"] is None


def run_guard():
    _self_check()
    if not AUDIT_FILE.exists():
        raise RuntimeError("dry_ground_temporal_audit.json bulunamadı.")
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
        print("Sentinel zaman-serisi yörünge koruması öz testi başarılı.")
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
        "Sentinel zaman-serisi yörünge koruması tamamlandı: "
        + (", ".join(parts) or "uygun bölge yok")
        + ". Üretim alarmı/görev değişmedi."
    )


if __name__ == "__main__":
    main()
