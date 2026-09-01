"""Kuru-zemin zaman serisinde tek sakin aralığa fazla güvenilmesini engeller.

`dry_ground_temporal_audit.py` 24->26 gibi tek bir değişim-öncesi aralığı 26->29 gibi
son değişimle karşılaştırır. Tarla sürümü / kuru zemin kullanımı bazı günlerde sakin,
bazı günlerde hareketli olabildiği için tek sakin aralık yanlış bir "ani başlangıç"
desteği verebilir.

Bu koruma, mevcut değişim-öncesi sahneden daha eski ve aynı MGRS / göreli yörüngedeki
bir Sentinel sahnesini bulur. Aynı 3x3 konumda ikinci bir eski BSI değişim aralığını
ölçer. İkinci eski aralık belirgin biçimde hareketliyse mevcut ani-başlangıç etiketini
yalnız kalibrasyon önceliğinde aşağı sınıflandırır. Eşik çevresindeki küçük farkları
kanıt saymaz ve yeni ani-başlangıç etiketi üretmez.

Üretim alarmı, saha görevi, 250 m² alt sınırı ve ana Sentinel eşikleri değişmez.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

import dry_ground_temporal_audit as temporal
import satellite


AUDIT_FILE = Path(__file__).with_name("dry_ground_temporal_audit.json")
# Bu katman sadece mevcut bir kanıtı düşürebildiği için ana eşikten daha konservatif
# davranır. 0.0800 ile 0.0805 gibi tek-piksel / yeniden örnekleme seviyesindeki farklar
# saha önceliğini değiştirmesin; eski hareket en az 0.10 BSI ya da güncel hareketin
# en az %60'ı kadar güçlü olmalı.
DOWNCLASS_ABS_BSI_MIN = 0.10
DOWNCLASS_RELATIVE_MIN = 0.60


def _patch_mean(delta, valid_mask, row_slice, col_slice):
    patch_valid = valid_mask[row_slice, col_slice]
    total = int(patch_valid.size)
    valid_count = int(patch_valid.sum())
    valid_fraction = valid_count / max(total, 1)
    if not valid_count:
        return None, valid_fraction
    patch = delta[row_slice, col_slice]
    return float(np.mean(patch[patch_valid])), valid_fraction


def _scene_orbits(items, scene_ids):
    orbits = {}
    for label, item_id in scene_ids.items():
        item = temporal._find_item(items, item_id)
        orbits[label] = satellite._relative_orbit(item) if item else None
    values = list(orbits.values())
    if any(value is None for value in values):
        return orbits, "BILINMIYOR"
    return orbits, "AYNI" if len(set(values)) == 1 else "FARKLI"


def _guard_row(row, older_baseline, older_valid_fraction):
    updated = dict(row)
    if older_baseline is None:
        updated["ikinci_onceki_donem_bsi_degisim"] = None
        updated["ikinci_onceki_donem_gecerli_oran"] = round(
            float(older_valid_fraction or 0), 3
        )
        updated["uzun_temporal_koruma"] = "YETERSIZ_GECERLI_PIKSEL"
        return updated, False

    current = abs(float(updated.get("son_cift_bsi_degisim") or 0))
    first_baseline = abs(float(updated.get("onceki_donem_bsi_degisim") or 0))
    second_baseline = abs(float(older_baseline))
    existing_valid = float(
        updated.get("uc_sahne_gecerli_oran")
        or updated.get("onceki_donem_gecerli_oran")
        or 0
    )
    older_valid = float(older_valid_fraction or 0)
    joint_valid = min(existing_valid, older_valid)
    baseline_max = max(first_baseline, second_baseline)

    updated["ikinci_onceki_donem_bsi_degisim"] = round(second_baseline, 4)
    updated["ikinci_onceki_donem_gecerli_oran"] = round(older_valid, 3)
    updated["temporal_baseline_maks_bsi_degisim"] = round(baseline_max, 4)

    # Ek eski sahnede 3x3 yamanın en az 6/9'u güvenilir değilse mevcut üç-sahne
    # kanıtını değiştirme. Eksik veri yanlış-pozitif kanıtı değildir; yalnız ek
    # doğrulamanın yapılamadığını gösterir.
    if joint_valid < temporal.MIN_VALID_FRACTION:
        updated["uzun_temporal_ani_baslangic_orani"] = None
        updated["uzun_temporal_istikrarsiz_zemin_riski"] = False
        updated["uzun_temporal_koruma"] = "YETERSIZ_GECERLI_PIKSEL"
        return updated, False

    ratio, abrupt_long, unstable_long = temporal._classify(
        current,
        baseline_max,
        joint_valid,
    )
    updated["uzun_temporal_ani_baslangic_orani"] = ratio
    updated["uzun_temporal_istikrarsiz_zemin_riski"] = bool(unstable_long)

    # Mevcut üç-sahne ani-başlangıç kanıtını ancak ikinci eski aralıkta belirgin
    # tekrar varsa düşür. Ana sınıflandırmadaki 0.08 sınırının milimetrik aşılması
    # tek başına yeterli değildir; bu koruma geri döndürülemez kanıt eklememeli.
    material_repeat = bool(
        second_baseline >= DOWNCLASS_ABS_BSI_MIN
        or (
            current > 0
            and second_baseline >= current * DOWNCLASS_RELATIVE_MIN
        )
    )
    downclassified = bool(updated.get("ani_baslangic_destegi")) and material_repeat
    if downclassified:
        updated["ani_baslangic_destegi"] = False
        updated["ani_baslangic_nedeni"] = (
            "IKINCI_ONCEKI_DONEM_BELIRGIN_TEKRAR_HAREKETI"
        )
        updated["uzun_temporal_koruma"] = "ASAGI_SINIFLANDIRILDI"
    elif bool(updated.get("ani_baslangic_destegi")) and not abrupt_long:
        # Ana 0.08 eşiği gibi sert bir sınır yalnız çok az aşılmış olabilir. Bu
        # durumu görünür kıl fakat mevcut saha önceliğini silme.
        updated["uzun_temporal_koruma"] = "SINIRDA_KORUNDU"
    else:
        updated["uzun_temporal_koruma"] = "KORUNDU"

    # Uzun geçmişte açık istikrarsızlık varsa mevcut kalibrasyon korumasını da
    # güçlendir. Bu yalnız alarm-dışı kalibrasyon önceliğini etkiler.
    updated["istikrarsiz_zemin_riski"] = bool(
        updated.get("istikrarsiz_zemin_riski") or unstable_long
    )
    return updated, downclassified


def _guard_region(region_key, region_data):
    if not isinstance(region_data, dict) or region_data.get("durum") != "ok":
        return region_data

    bbox = satellite.REGIONS[region_key]["bbox"]
    items = satellite._search_items(bbox)
    anchor = temporal._find_item(items, region_data.get("degisim_oncesi_item"))
    if anchor is None:
        region_data["uzun_temporal_durum"] = "ATLANDI"
        region_data["uzun_temporal_neden"] = "degisim_oncesi_sahne_bulunamadi"
        return region_data

    earlier = temporal._previous_scene(items, anchor, bbox)
    if earlier is None:
        region_data["uzun_temporal_durum"] = "ATLANDI"
        region_data["uzun_temporal_neden"] = "ikinci_onceki_sahne_bulunamadi"
        return region_data

    scene_ids = {
        "ikinci_onceki": earlier.get("id"),
        "degisim_oncesi": region_data.get("degisim_oncesi_item"),
        "onceki": region_data.get("onceki_item"),
        "son": region_data.get("son_item"),
    }
    orbits, orbit_state = _scene_orbits(items, scene_ids)
    region_data["uzun_temporal_goreli_yorungeler"] = orbits
    region_data["uzun_temporal_yorunge_tutarliligi"] = orbit_state
    region_data["ikinci_onceki_item"] = earlier.get("id")
    region_data["ikinci_onceki_tarih"] = satellite._item_date(earlier)

    # Ek geçmiş aralığı yalnız dört sahnenin göreli yörüngesi açıkça aynıysa
    # sınıflandırmaya uygula. Bilinmeyen/farklı geometride mevcut üç-sahne kanıtını
    # değiştirmeyerek güvenli geri dönüş yap.
    if orbit_state != "AYNI":
        region_data["uzun_temporal_durum"] = "SADECE_DIAGNOSTIK"
        region_data["uzun_temporal_neden"] = (
            "dort_sahne_goreli_yorunge_dogrulanamadi"
        )
        return region_data

    height, width = satellite._output_shape(bbox)
    earlier_bsi, earlier_scl = temporal._bsi_for_item(
        earlier, bbox, height, width
    )
    anchor_bsi, anchor_scl = temporal._bsi_for_item(
        anchor, bbox, height, width
    )
    older_delta = np.abs(anchor_bsi - earlier_bsi)
    valid = ~np.isin(earlier_scl, satellite.EXCLUDED_SCL_CLASSES)
    valid &= ~np.isin(anchor_scl, satellite.EXCLUDED_SCL_CLASSES)

    rows = []
    downclassified = 0
    borderline = 0
    measured = 0
    for raw in region_data.get("adaylar") or []:
        if not isinstance(raw, dict):
            continue
        row, column = temporal._pixel_for_point(
            raw.get("enlem"), raw.get("boylam"), bbox, older_delta.shape
        )
        row_slice, col_slice = temporal._patch_slices(
            row, column, older_delta.shape
        )
        older_mean, older_valid_fraction = _patch_mean(
            older_delta,
            valid,
            row_slice,
            col_slice,
        )
        if older_mean is not None:
            measured += 1
        updated, was_downclassified = _guard_row(
            raw,
            older_mean,
            older_valid_fraction,
        )
        if was_downclassified:
            downclassified += 1
        if updated.get("uzun_temporal_koruma") == "SINIRDA_KORUNDU":
            borderline += 1
        rows.append(updated)

    region_data["adaylar"] = rows
    region_data["uzun_temporal_durum"] = "UYGULANDI"
    region_data["uzun_temporal_olculen_aday"] = measured
    region_data["uzun_temporal_asagi_siniflanan"] = downclassified
    region_data["uzun_temporal_sinirda_korunan"] = borderline
    region_data["ani_baslangic_destegi"] = sum(
        1
        for row in rows
        if isinstance(row, dict) and bool(row.get("ani_baslangic_destegi"))
    )
    region_data["istikrarsiz_zemin_riski"] = sum(
        1
        for row in rows
        if isinstance(row, dict) and bool(row.get("istikrarsiz_zemin_riski"))
    )
    return region_data


def guard_payload(payload):
    if not isinstance(payload, dict):
        return payload
    regions = payload.get("bolgeler") or {}
    if not isinstance(regions, dict):
        return payload

    for region_key, region_data in list(regions.items()):
        if region_key not in satellite.REGIONS:
            continue
        try:
            regions[region_key] = _guard_region(region_key, region_data)
        except Exception as exc:
            if isinstance(region_data, dict):
                region_data["uzun_temporal_durum"] = "HATA"
                region_data["uzun_temporal_neden"] = str(exc)
    payload["uzun_temporal_koruma_notu"] = (
        "İkinci bir eski Sentinel aralığı yalnız aynı göreli yörünge, yeterli geçerli "
        "piksel ve belirgin tekrar hareketi doğrulandığında mevcut ani-başlangıç "
        "desteğini aşağı sınıflandırabilir. Eşik çevresindeki küçük farklar ve yetersiz "
        "veri mevcut kanıtı değiştirmez; yeni alarm veya yeni ani-başlangıç etiketi "
        "üretilmez."
    )
    payload["uzun_temporal_dusurme_esikleri"] = {
        "ikinci_onceki_mutlak_bsi_min": DOWNCLASS_ABS_BSI_MIN,
        "ikinci_onceki_guncel_harekete_oran_min": DOWNCLASS_RELATIVE_MIN,
    }
    return payload


def _self_check():
    stable = {
        "son_cift_bsi_degisim": 0.24,
        "onceki_donem_bsi_degisim": 0.03,
        "uc_sahne_gecerli_oran": 1.0,
        "ani_baslangic_destegi": True,
        "istikrarsiz_zemin_riski": False,
    }
    guarded, down = _guard_row(stable, 0.04, 1.0)
    assert guarded["ani_baslangic_destegi"] is True
    assert down is False

    repeated = dict(stable)
    guarded, down = _guard_row(repeated, 0.13, 1.0)
    assert guarded["ani_baslangic_destegi"] is False
    assert down is True
    assert guarded["istikrarsiz_zemin_riski"] is False

    unstable = dict(stable)
    guarded, down = _guard_row(unstable, 0.20, 1.0)
    assert guarded["ani_baslangic_destegi"] is False
    assert guarded["istikrarsiz_zemin_riski"] is True
    assert down is True

    # 0.08 ana eşiğini yalnız 0.0005 aşan eski hareket Sentinel ölçüm marjı içinde
    # kabul edilir; mevcut güçlü saha önceliği korunur ve yalnız sınırda işaretlenir.
    borderline = {
        "son_cift_bsi_degisim": 0.306,
        "onceki_donem_bsi_degisim": 0.0404,
        "uc_sahne_gecerli_oran": 0.778,
        "ani_baslangic_destegi": True,
        "istikrarsiz_zemin_riski": False,
    }
    guarded, down = _guard_row(borderline, 0.0805, 0.778)
    assert guarded["ani_baslangic_destegi"] is True
    assert guarded["uzun_temporal_koruma"] == "SINIRDA_KORUNDU"
    assert down is False

    # Ek eski sahne düşük kaliteli olduğunda mevcut üç-sahne kanıtını silmemeliyiz.
    insufficient = dict(stable)
    guarded, down = _guard_row(insufficient, 0.03, 5 / 9)
    assert guarded["ani_baslangic_destegi"] is True
    assert guarded["uzun_temporal_koruma"] == "YETERSIZ_GECERLI_PIKSEL"
    assert down is False


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
        print("Uzun zaman-serisi taban koruması öz testi başarılı.")
        return

    payload = run_guard()
    parts = []
    for region_key, data in (payload.get("bolgeler") or {}).items():
        if not isinstance(data, dict) or data.get("durum") != "ok":
            continue
        parts.append(
            f"{region_key}={data.get('uzun_temporal_durum')} "
            f"(ani={int(data.get('ani_baslangic_destegi') or 0)}, "
            f"asagi={int(data.get('uzun_temporal_asagi_siniflanan') or 0)}, "
            f"sinirda={int(data.get('uzun_temporal_sinirda_korunan') or 0)})"
        )
    print(
        "Uzun zaman-serisi taban koruması tamamlandı: "
        + (", ".join(parts) or "uygun bölge yok")
        + ". Alarm/görev üretilmedi."
    )


if __name__ == "__main__":
    main()
