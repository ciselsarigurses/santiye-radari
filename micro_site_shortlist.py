"""150-249 m² ham mikro adayları saha açısından daha kullanışlı diagnostik kısa listeye indirger.

Ham mikro tarama özellikle Çeşme/Uzunkuyu kutularının örtüşmesinde aynı fiziksel
noktayı iki kez görebilir; ayrıca geniş tarla/zemin değişiminin kenarları iki-piksel
parçacıklar olarak çok sayıda mikro aday üretebilir. Bu koruma:

1) yaklaşık 30 m içinde bölge-örtüşmesi tekrarlarını tekilleştirir,
2) 250 m çevresinde yoğun mikro kümelerini geniş-yüzey riski sayar,
3) mevcut 250 m²+ üretim adayına 150 m'den yakın mikro parçayı ayrı fırsat saymaz,
4) yalnız daha izole ve spektral olarak güçlü adayları alarm-dışı kısa listeye alır.

Alarm, saha görevi ve ana 250 m² üretim eşiği değişmez.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np


RAW_FILE = Path(__file__).with_name("micro_site_audit.json")
LATEST_REPORT_FILE = Path(__file__).with_name("latest_report.json")
OUTPUT_FILE = Path(__file__).with_name("micro_site_shortlist.json")

DEDUPE_RADIUS_M = 30
PRODUCTION_NEAR_RADIUS_M = 150
CLUSTER_RADIUS_M = 250
MAX_CLUSTER_NEIGHBORS = 2
MIN_RGB_CHANGE = 0.24
MIN_NDVI_LOSS = 0.22
SHORTLIST_LIMIT = 12


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _distance_m(first, second):
    lat1 = _number(first.get("enlem"), None)
    lon1 = _number(first.get("boylam"), None)
    lat2 = _number(second.get("enlem"), None)
    lon2 = _number(second.get("boylam"), None)
    if None in (lat1, lon1, lat2, lon2):
        return float("inf")
    mean_lat = (lat1 + lat2) / 2
    north_m = (lat2 - lat1) * 110570
    east_m = (lon2 - lon1) * 111320 * np.cos(np.radians(mean_lat))
    return float(np.hypot(north_m, east_m))


def _strength(item):
    return (
        _number(item.get("ortalama_rgb_degisim"), 0.0) * 1.0
        + _number(item.get("ortalama_ndvi_kaybi"), 0.0) * 0.7
        + _number(item.get("ortalama_parlaklik_artisi"), 0.0) * 0.3
    )


def _raw_candidates(payload):
    rows = []
    for region_key, region in (payload.get("bolgeler") or {}).items():
        if not isinstance(region, dict):
            continue
        for item in region.get("adaylar") or []:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row.setdefault("bolge", region_key)
            rows.append(row)
    return rows


def _dedupe(rows):
    """Örtüşen Sentinel kutularındaki aynı fiziksel mikro sinyali tekilleştir."""
    kept = []
    duplicate_count = 0
    for row in sorted(rows, key=_strength, reverse=True):
        duplicate = next(
            (existing for existing in kept if _distance_m(row, existing) <= DEDUPE_RADIUS_M),
            None,
        )
        if duplicate is not None:
            duplicate_count += 1
            regions = set(duplicate.get("kaynak_bolgeler") or [duplicate.get("bolge")])
            regions.add(row.get("bolge"))
            duplicate["kaynak_bolgeler"] = sorted(value for value in regions if value)
            duplicate["ortusme_tekrari"] = True
            continue
        candidate = dict(row)
        candidate["kaynak_bolgeler"] = [candidate.get("bolge")]
        candidate["ortusme_tekrari"] = False
        kept.append(candidate)
    return kept, duplicate_count


def _production_candidates():
    if not LATEST_REPORT_FILE.exists():
        return []
    try:
        payload = json.loads(LATEST_REPORT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("saha_adaylari") or []
    return [row for row in rows if isinstance(row, dict)]


def _nearest_distance(row, others):
    distances = [_distance_m(row, other) for other in others]
    finite = [distance for distance in distances if np.isfinite(distance)]
    return min(finite) if finite else None


def _annotate(rows, production):
    annotated = []
    for index, row in enumerate(rows):
        others = [other for other_index, other in enumerate(rows) if other_index != index]
        neighbor_count = sum(
            _distance_m(row, other) <= CLUSTER_RADIUS_M for other in others
        )
        production_distance = _nearest_distance(row, production)
        updated = dict(row)
        updated["250m_mikro_komsu"] = int(neighbor_count)
        updated["genis_hareket_kumesi_riski"] = bool(
            neighbor_count > MAX_CLUSTER_NEIGHBORS
        )
        updated["en_yakin_250plus_m"] = (
            round(production_distance) if production_distance is not None else None
        )
        updated["ana_adaya_yakin"] = bool(
            production_distance is not None
            and production_distance <= PRODUCTION_NEAR_RADIUS_M
        )
        updated["spektral_kisa_liste_kapisi"] = bool(
            _number(updated.get("ortalama_rgb_degisim"), 0.0) >= MIN_RGB_CHANGE
            and _number(updated.get("ortalama_ndvi_kaybi"), 0.0) >= MIN_NDVI_LOSS
        )
        annotated.append(updated)
    return annotated


def build_shortlist(raw_payload, latest_report_exists=True):
    raw_rows = _raw_candidates(raw_payload)
    deduped, duplicate_count = _dedupe(raw_rows)
    production = _production_candidates() if latest_report_exists else []
    annotated = _annotate(deduped, production)

    shortlist = [
        row for row in annotated
        if not row["genis_hareket_kumesi_riski"]
        and not row["ana_adaya_yakin"]
        and row["spektral_kisa_liste_kapisi"]
    ]
    shortlist.sort(
        key=lambda row: (
            row["250m_mikro_komsu"],
            -_strength(row),
            _number(row.get("alan_m2"), 9999),
        )
    )
    shortlist = shortlist[:SHORTLIST_LIMIT]

    background = [
        row for row in annotated
        if row not in shortlist
    ]
    gulbahce_shortlist = sum(
        str(row.get("yaklasik_mevki") or "").startswith("Gülbahçe")
        for row in shortlist
    )
    gulbahce_raw = sum(
        str(row.get("yaklasik_mevki") or "").startswith("Gülbahçe")
        for row in deduped
    )

    return {
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": raw_payload.get("ana_uretim_esigi_m2", 250),
        "mikro_aralik_m2": raw_payload.get("mikro_aralik_m2", [150, 249]),
        "kaynak_olusturma": raw_payload.get("olusturma"),
        "ham_aday": len(raw_rows),
        "tekil_aday": len(deduped),
        "ortusme_tekrari_elendi": duplicate_count,
        "genis_hareket_kumesi_arka_plana_alindi": sum(
            row["genis_hareket_kumesi_riski"] for row in annotated
        ),
        "ana_250plus_adaya_yakin_arka_plana_alindi": sum(
            row["ana_adaya_yakin"] for row in annotated
        ),
        "gulbahce_ham_tekil_aday": gulbahce_raw,
        "gulbahce_kisa_liste_aday": gulbahce_shortlist,
        "kisa_liste": shortlist,
        "arka_plan_aday_sayisi": len(background),
        "not": (
            "Kısa liste de alarm/görev değildir. Yalnız 150-249 m² ham mikro havuzdaki "
            "örtüşme tekrarları, geniş yüzey kümeleri ve mevcut 250+ aday parçaları "
            "ayıklanmıştır. Saha rotasına geçmek için ayrıca temporal devam/ani başlangıç "
            "veya güvenilir açık-web/yapılaşma doğrulaması gerekir."
        ),
    }


def _self_check():
    synthetic = {
        "ana_uretim_esigi_m2": 250,
        "mikro_aralik_m2": [150, 249],
        "bolgeler": {
            "cesme": {
                "adaylar": [
                    {
                        "bolge": "cesme", "enlem": 38.3, "boylam": 26.4,
                        "alan_m2": 200, "ortalama_rgb_degisim": 0.5,
                        "ortalama_ndvi_kaybi": 0.4,
                        "ortalama_parlaklik_artisi": 0.2,
                    }
                ]
            },
            "uzunkuyu": {
                "adaylar": [
                    {
                        "bolge": "uzunkuyu", "enlem": 38.30005, "boylam": 26.40005,
                        "alan_m2": 200, "ortalama_rgb_degisim": 0.45,
                        "ortalama_ndvi_kaybi": 0.35,
                        "ortalama_parlaklik_artisi": 0.2,
                    }
                ]
            },
        },
    }
    rows = _raw_candidates(synthetic)
    deduped, duplicates = _dedupe(rows)
    assert len(rows) == 2
    assert len(deduped) == 1
    assert duplicates == 1
    assert deduped[0]["ortusme_tekrari"] is True


def main():
    _self_check()
    if not RAW_FILE.exists():
        raise RuntimeError("micro_site_audit.json bulunamadı.")
    payload = json.loads(RAW_FILE.read_text(encoding="utf-8"))
    result = build_shortlist(payload)
    OUTPUT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Mikro kısa liste hazır: "
        f"ham={result['ham_aday']}, tekil={result['tekil_aday']}, "
        f"kısa_liste={len(result['kisa_liste'])}, "
        f"Gülbahçe={result['gulbahce_kisa_liste_aday']}. Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
