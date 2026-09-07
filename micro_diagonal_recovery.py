"""Diyagonal 8-komşulukta yutulan 150-249 m² MİKRO parçaları geri kazanır.

Ana Sentinel üretimi 8-komşu ve 250 m² eşikle aynen kalır. Bu sidecar yalnız zaten
MİKRO katmanın güçlü çoklu-spektral maskesini geçen pikselleri yeniden 4-komşulukla
okur. 150-249 m² kompakt bir 4-komşu parça, sırf köşeden başka güçlü piksellere
temas ettiği için 8-komşulukta 250 m²+ bir ebeveyne birleşmişse diagnostik havuza
geri eklenir.

İkinci aşamada mevcut MİKRO kısa liste yakınlık koruması yalnız bu açıkça işaretli
geometrik kurtarmalarda tek başına eleme nedeni olmaktan çıkar. Geniş-yüzey koruması
ve daha sert kısa-liste spektral kapısı aynen korunur; temporal/lokal/footprint zinciri
daha sonra yine zorunludur. Alarm veya saha görevi üretilmez.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

import satellite
import micro_site_audit as micro
import micro_site_shortlist as shortlist
from candidate_capacity_audit import _four_connected_components


RAW_FILE = Path(__file__).with_name("micro_site_audit.json")
SHORTLIST_FILE = Path(__file__).with_name("micro_site_shortlist.json")
SOURCE_NAME = "4_komsu_diyagonal_mikro_kurtarma"


def _is_recovery(item):
    return bool((item or {}).get("diyagonal_ebeveynden_ayrisan")) or str(
        (item or {}).get("geometri_kaynagi") or ""
    ) == SOURCE_NAME


def _component_recoveries(mask, pixel_area_m2):
    """250+ 8-komşu ebeveyn içindeki geçerli 150-249 m² 4-komşu parçaları döndürür."""
    eight_components = satellite._connected_components(mask)
    parent_by_pixel = {}
    parent_area = {}
    for parent_index, component in enumerate(eight_components):
        parent_area[parent_index] = len(component) * pixel_area_m2
        for pixel in component:
            parent_by_pixel[pixel] = parent_index

    recovered = []
    for component in _four_connected_components(mask):
        if len(component) < micro.MICRO_MIN_PIXELS:
            continue
        area_m2 = len(component) * pixel_area_m2
        if not (micro.MICRO_MIN_AREA_M2 <= area_m2 < micro.MICRO_MAX_AREA_M2):
            continue
        bbox_pixels, fill_ratio = micro._compactness(component)
        if (
            bbox_pixels > micro.MICRO_MAX_BBOX_PIXELS
            or fill_ratio < micro.MICRO_MIN_FILL_RATIO
        ):
            continue
        parent_index = parent_by_pixel.get(component[0])
        if parent_index is None:
            continue
        parent_area_m2 = parent_area.get(parent_index, 0.0)
        if parent_area_m2 < satellite.MIN_HOTSPOT_AREA_M2:
            continue
        recovered.append(
            {
                "component": component,
                "area_m2": area_m2,
                "bbox_pixels": bbox_pixels,
                "fill_ratio": fill_ratio,
                "parent_area_m2": parent_area_m2,
            }
        )
    return recovered


def _recovery_rows(region_key, expected_region=None):
    bbox = satellite.REGIONS[region_key]["bbox"]
    pair = satellite.sentinel_pair(region_key)
    older, latest = pair
    if expected_region:
        expected_older = str(expected_region.get("onceki_item") or "")
        expected_latest = str(expected_region.get("son_item") or "")
        if expected_older and expected_older != str(older.get("id") or ""):
            raise RuntimeError(
                f"{region_key}: mikro audit önceki sahnesi güncel Sentinel çiftiyle eşleşmiyor."
            )
        if expected_latest and expected_latest != str(latest.get("id") or ""):
            raise RuntimeError(
                f"{region_key}: mikro audit son sahnesi güncel Sentinel çiftiyle eşleşmiyor."
            )

    height, width = satellite._output_shape(bbox)
    strict, rgb_difference, vegetation_loss, brightness_gain = micro._strict_micro_mask(
        older, latest, bbox, height, width
    )
    pixel_area = micro._pixel_area_m2(bbox, height, width)

    rows = []
    for recovered in _component_recoveries(strict, pixel_area):
        component = recovered["component"]
        pixels = np.asarray(component, dtype="int32")
        latitude, longitude = micro._representative_point(component, bbox, strict.shape)
        gulbahce_distance = micro._distance_m(
            latitude, longitude, micro.GULBAHCE_REFERENCE_POINT
        )
        rows.append(
            {
                "oncelik": "MIKRO_DIAGNOSTIK",
                "alarm": False,
                "saha_gorevi": False,
                "bolge": region_key,
                "yaklasik_mevki": (
                    "Gülbahçe çevresi"
                    if gulbahce_distance <= micro.GULBAHCE_LABEL_RADIUS_M
                    else satellite._nearest_place(latitude, longitude)
                ),
                "enlem": latitude,
                "boylam": longitude,
                "alan_m2": round(recovered["area_m2"]),
                "piksel": len(component),
                "bbox_piksel": recovered["bbox_pixels"],
                "doluluk_orani": round(recovered["fill_ratio"], 3),
                "ortalama_rgb_degisim": round(
                    float(np.mean(rgb_difference[pixels[:, 0], pixels[:, 1]])), 4
                ),
                "ortalama_ndvi_kaybi": round(
                    float(np.mean(vegetation_loss[pixels[:, 0], pixels[:, 1]])), 4
                ),
                "ortalama_parlaklik_artisi": round(
                    float(np.mean(brightness_gain[pixels[:, 0], pixels[:, 1]])), 4
                ),
                "gulbahce_merkeze_mesafe_m": round(gulbahce_distance),
                "harita": (
                    "https://www.google.com/maps/dir/?api=1&destination="
                    f"{latitude:.6f},{longitude:.6f}"
                ),
                "geometri_kaynagi": SOURCE_NAME,
                "diyagonal_ebeveynden_ayrisan": True,
                "sekiz_komsu_ebeveyn_alan_m2": round(recovered["parent_area_m2"]),
                "neden": (
                    "Güçlü MİKRO maskesinde 150-249 m² kompakt 4-komşu parça, yalnız "
                    "köşe teması nedeniyle 8-komşulukta 250 m²+ ebeveyne birleşiyordu. "
                    "Geometrik diagnostik olarak geri kazanıldı; tek başına alarm veya "
                    "saha ziyareti nedeni değildir."
                ),
            }
        )

    rows.sort(
        key=lambda item: (
            -float(item["ortalama_rgb_degisim"]),
            -float(item["ortalama_ndvi_kaybi"]),
            float(item["alan_m2"]),
            item["enlem"],
            item["boylam"],
        )
    )
    return rows


def _candidate_key(item):
    try:
        return (
            str(item.get("bolge") or ""),
            round(float(item.get("enlem")), 6),
            round(float(item.get("boylam")), 6),
            round(float(item.get("alan_m2")), 1),
        )
    except (TypeError, ValueError, AttributeError):
        return None


def apply_audit_sidecar():
    if not RAW_FILE.exists():
        raise RuntimeError("micro_site_audit.json bulunamadı.")
    payload = json.loads(RAW_FILE.read_text(encoding="utf-8"))
    if int(payload.get("ana_uretim_esigi_m2") or 0) != 250:
        raise RuntimeError("Ana üretim eşiği 250 m² değil; sidecar güvenli biçimde durdu.")
    if list(payload.get("mikro_aralik_m2") or []) != [150, 249]:
        raise RuntimeError("MİKRO bandı 150-249 m² değil; sidecar güvenli biçimde durdu.")

    total = 0
    for region_key in micro.REPORT_REGIONS:
        region = (payload.get("bolgeler") or {}).get(region_key)
        if not isinstance(region, dict) or region.get("durum") != "ok":
            continue

        base_rows = [
            row for row in (region.get("adaylar") or [])
            if isinstance(row, dict) and not _is_recovery(row)
        ]
        recovered_rows = _recovery_rows(region_key, expected_region=region)
        existing_keys = {key for key in map(_candidate_key, base_rows) if key is not None}
        unique_recovered = []
        for row in recovered_rows:
            key = _candidate_key(row)
            if key is None or key in existing_keys:
                continue
            existing_keys.add(key)
            unique_recovered.append(row)

        region["adaylar"] = base_rows + unique_recovered
        region["aday_sayisi"] = len(region["adaylar"])
        region["diyagonal_mikro_kurtarma_sayisi"] = len(unique_recovered)
        total += len(unique_recovered)

        if region_key == "uzunkuyu":
            observability = region.get("gulbahce_gozlenebilirlik") or {}
            if observability:
                observability["mikro_ham_aday_2km"] = sum(
                    micro._distance_m(
                        float(row["enlem"]),
                        float(row["boylam"]),
                        micro.GULBAHCE_OPERATION_POINT,
                    ) <= micro.GULBAHCE_OPERATION_RADIUS_M
                    for row in region["adaylar"]
                    if isinstance(row, dict)
                    and row.get("enlem") is not None
                    and row.get("boylam") is not None
                )
                observability["diyagonal_mikro_kurtarma_2km"] = sum(
                    _is_recovery(row)
                    and micro._distance_m(
                        float(row["enlem"]),
                        float(row["boylam"]),
                        micro.GULBAHCE_OPERATION_POINT,
                    ) <= micro.GULBAHCE_OPERATION_RADIUS_M
                    for row in region["adaylar"]
                    if isinstance(row, dict)
                    and row.get("enlem") is not None
                    and row.get("boylam") is not None
                )

    payload["diyagonal_mikro_kurtarma_toplam"] = total
    payload["diyagonal_mikro_kurtarma_alarm"] = False
    payload["diyagonal_mikro_kurtarma_saha_gorevi"] = False
    payload["diyagonal_mikro_kurtarma_not"] = (
        "Yalnız güçlü MİKRO maskesinde, köşe teması yüzünden 8-komşu 250+ ebeveyne "
        "yutulan kompakt 150-249 m² 4-komşu parçalar diagnostik olarak geri kazanılır. "
        "Ana 250 m² üretim alarmı ve 8-komşu üretim geometrisi değişmez."
    )
    RAW_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def _sidecar_shortlist_eligible(row):
    return bool(
        _is_recovery(row)
        and not row.get("genis_hareket_kumesi_riski")
        and row.get("spektral_kisa_liste_kapisi")
    )


def apply_shortlist_sidecar():
    if not RAW_FILE.exists() or not SHORTLIST_FILE.exists():
        raise RuntimeError("MİKRO ham audit veya kısa liste JSON'u bulunamadı.")
    raw_payload = json.loads(RAW_FILE.read_text(encoding="utf-8"))
    payload = json.loads(SHORTLIST_FILE.read_text(encoding="utf-8"))
    if int(payload.get("ana_uretim_esigi_m2") or 0) != 250:
        raise RuntimeError("Kısa listedeki ana üretim eşiği 250 m² değil.")

    raw_rows = shortlist._raw_candidates(raw_payload)
    deduped, _ = shortlist._dedupe(raw_rows)
    source_dates = shortlist._current_source_dates(raw_payload)
    production_all = shortlist._production_candidates()
    production = shortlist._filter_current_production(production_all, source_dates)
    annotated = shortlist._annotate(deduped, production)

    recoveries = []
    for row in annotated:
        if not _sidecar_shortlist_eligible(row):
            continue
        candidate = dict(row)
        candidate["diyagonal_yakinlik_istisnasi"] = bool(candidate.get("ana_adaya_yakin"))
        candidate["diyagonal_yakinlik_istisnasi_nedeni"] = (
            "Bu kayıt, 4-komşu 150-249 m² parçanın yalnız köşe temasıyla 250+ "
            "8-komşu ebeveyne birleşmesi nedeniyle ana adaya yakındır. Yakınlık tek "
            "başına eleme nedeni yapılmadı; spektral ve geniş-yüzey kapıları korundu."
        )
        candidate["alarm"] = False
        candidate["saha_gorevi"] = False
        recoveries.append(candidate)

    current = [row for row in (payload.get("kisa_liste") or []) if isinstance(row, dict)]
    by_key = {}
    for row in current + recoveries:
        key = _candidate_key(row)
        if key is None:
            continue
        old = by_key.get(key)
        if old is None or shortlist._strength(row) > shortlist._strength(old):
            by_key[key] = row

    combined = list(by_key.values())
    combined.sort(
        key=lambda row: (
            int(row.get("250m_mikro_komsu") or 0),
            -shortlist._strength(row),
            shortlist._number(row.get("alan_m2"), 9999),
        )
    )
    combined = combined[: shortlist.SHORTLIST_LIMIT]
    payload["kisa_liste"] = combined
    payload["arka_plan_aday_sayisi"] = max(
        int(payload.get("tekil_aday") or len(deduped)) - len(combined), 0
    )
    payload["diyagonal_mikro_kurtarma_tekil"] = sum(_is_recovery(row) for row in deduped)
    payload["diyagonal_mikro_kurtarma_kisa_liste"] = sum(
        _is_recovery(row) for row in combined
    )
    payload["diyagonal_mikro_yakinlik_istisnasi"] = sum(
        bool(row.get("diyagonal_yakinlik_istisnasi")) for row in combined
    )
    payload["gulbahce_kisa_liste_aday"] = sum(
        str(row.get("yaklasik_mevki") or "").startswith("Gülbahçe") for row in combined
    )
    sidecar_note = (
        " Köşe teması nedeniyle 250+ ebeveyne birleşmiş açıkça işaretli 4-komşu "
        "MİKRO kurtarmalarda ana-adaya yakınlık tek başına eleme değildir; geniş-yüzey "
        "ve güçlü spektral kısa-liste kapıları ile sonraki temporal/lokal doğrulamalar "
        "zorunlu kalır."
    )
    note = str(payload.get("not") or "")
    if "Köşe teması nedeniyle 250+ ebeveyne" not in note:
        payload["not"] = note + sidecar_note

    SHORTLIST_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def _self_check():
    assert satellite.MIN_HOTSPOT_AREA_M2 == 250
    assert micro.MICRO_MIN_AREA_M2 == 150
    assert micro.MICRO_MAX_AREA_M2 == 250

    mask = np.zeros((7, 7), dtype=bool)
    # 200 m² yatay parça, 300 m² parçaya yalnız köşeden dokunuyor. 8-komşuda
    # tek 500 m² ebeveyn; 4-komşuda 200 + 300 m² olmalı. Yalnız 200 geri alınır.
    mask[1, 1:3] = True
    mask[2, 3:5] = True
    mask[3, 3] = True
    recovered = _component_recoveries(mask, 100.0)
    assert len(recovered) == 1, recovered
    assert round(recovered[0]["area_m2"]) == 200, recovered
    assert round(recovered[0]["parent_area_m2"]) == 500, recovered

    recovery = {
        "geometri_kaynagi": SOURCE_NAME,
        "diyagonal_ebeveynden_ayrisan": True,
        "genis_hareket_kumesi_riski": False,
        "ana_adaya_yakin": True,
        "spektral_kisa_liste_kapisi": True,
    }
    assert _sidecar_shortlist_eligible(recovery)
    normal = dict(recovery)
    normal["geometri_kaynagi"] = "8_komsu_mikro"
    normal["diyagonal_ebeveynden_ayrisan"] = False
    assert not _sidecar_shortlist_eligible(normal)
    broad = dict(recovery)
    broad["genis_hareket_kumesi_riski"] = True
    assert not _sidecar_shortlist_eligible(broad)
    weak = dict(recovery)
    weak["spektral_kisa_liste_kapisi"] = False
    assert not _sidecar_shortlist_eligible(weak)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--augment-audit", action="store_true")
    parser.add_argument("--augment-shortlist", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print(
            "Diyagonal MİKRO sidecar öz testi başarılı: 250 m² ana eşik değişmeden "
            "yalnız güçlü/kompakt 4-komşu 150-249 m² parçalar geri kazanılıyor."
        )
        return
    if args.augment_audit:
        payload = apply_audit_sidecar()
        print(
            "Diyagonal MİKRO audit sidecar tamamlandı: "
            f"kurtarma={payload.get('diyagonal_mikro_kurtarma_toplam', 0)}. "
            "Alarm/görev üretilmedi."
        )
        return
    if args.augment_shortlist:
        payload = apply_shortlist_sidecar()
        print(
            "Diyagonal MİKRO kısa liste sidecar tamamlandı: "
            f"tekil={payload.get('diyagonal_mikro_kurtarma_tekil', 0)}, "
            f"kısa_liste={payload.get('diyagonal_mikro_kurtarma_kisa_liste', 0)}. "
            "Alarm/görev üretilmedi."
        )
        return
    parser.error("--check-only, --augment-audit veya --augment-shortlist seçilmelidir.")


if __name__ == "__main__":
    main()
