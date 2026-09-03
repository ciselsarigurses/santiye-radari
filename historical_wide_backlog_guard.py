"""Tarihsel taşınmış geniş-yüzey görevlerini güncel şekil kanıtıyla arka plana ayırır.

Günlük raporda eski Sentinel sahnelerinden taşınan 10.000 m² üstü görevler, yeni
sahnede aynı koordinat çevresinde yeniden düşük-kompaktlık geniş yüzey olarak
ölçülse bile operasyon backlog'unda kalabiliyordu. Ana geniş-yüzey koruması yalnız
güncel nihai seçim provenansını kullandığı için bu eski görevler rota ve gecikme
sayısını şişirebiliyordu.

Bu katman yalnız ``yeni_goruntu=false`` olan tarihsel/gecikmiş görevleri ele alır.
Aynı günkü ``shape_false_positive_audit`` üretim seçiminde 10.000 m² üstü,
düşük-kompaktlık geometriyle en fazla 25 m ve alan benzerliği en az 0.60 koşulunda
eşleşirse kaydı silmeden arka-plan izleme listesine taşır. Taze Sentinel görevleri,
TEKRAR_GIT kayıtları, 250 m² ana eşik ve 150-249 m² Mikro Şantiye katmanı değişmez.
Yeni sahnede kompakt/parsel ölçekli kanıt oluşursa bu eşleşme kurulmayacağı için görev
operasyon havuzunda kalabilir.
"""

from __future__ import annotations

import argparse
import json

import wide_surface_background_guard as base
import wide_surface_provenance_gap_guard as provenance


REPORT_JSON = provenance.REPORT_JSON
SOURCE_NAME = "shape_false_positive_audit_tarihsel_tasima"
DEFAULT_MATCH_METERS = 25.0
DEFAULT_MIN_AREA_SIMILARITY = 0.60
MIN_WAIT_DAYS = 1


def _historical_candidate(item):
    """Yalnız eski/gecikmiş ve geniş operasyon kaydını değerlendir."""
    if not isinstance(item, dict) or base._manual_repeat(item):
        return False
    if item.get("yeni_goruntu") is not False:
        return False
    area = max(base._number(item.get("alan_m2"), 0.0) or 0.0, 0.0)
    if area <= base.BACKGROUND_MIN_M2:
        return False
    wait_days = int(base._number(item.get("bekleme_gun"), 0) or 0)
    historical_match = base._number(item.get("tarihsel_esleme_mesafe_m")) is not None
    overdue = item.get("gecikmis") is True and wait_days >= MIN_WAIT_DAYS
    return bool(historical_match or overdue)


def _background_from_match(
    item,
    example,
    region_key,
    region_label,
    distance,
    similarity,
):
    compactness = float(base._number(example.get("kompaktlik"), 0.0) or 0.0)
    normalized = {
        **item,
        "genis_kompaktlik": compactness,
        "genis_geometri_riski": True,
        "sinyal": (
            "Güncel şekil denetiminde yeniden doğrulanan tarihsel geniş "
            "düşük-kompaktlık yüzey hareketi"
        ),
        "kanit_kaynagi": SOURCE_NAME,
    }
    payload = base._candidate_payload(normalized, region_key, region_label)
    if payload is None:
        return None
    payload["geometri_esleme_mesafe_m"] = round(float(distance), 1)
    payload["geometri_alan_benzerligi"] = round(float(similarity), 3)
    payload["geometri_kaynagi_enlem"] = round(float(example.get("enlem")), 6)
    payload["geometri_kaynagi_boylam"] = round(float(example.get("boylam")), 6)
    payload["tarihsel_gorev_id"] = str(item.get("gorev_id") or "")
    payload["tarihsel_bekleme_gun"] = int(base._number(item.get("bekleme_gun"), 0) or 0)
    payload["neden"] = (
        "Eski Sentinel kanıtından taşınmış 10.000 m² üstü görev, güncel aynı-gün şekil "
        "denetiminde yeniden düşük-kompaktlık geniş yüzey karakteri gösterdi. "
        f"Eşleşme mesafesi {distance:.1f} m, alan benzerliği {similarity:.2f}. Bu nedenle "
        "rota/backlog'u doldurmak yerine tarla/toprak temizliği/doğal veya başka geniş "
        "arazi hareketi olasılığı için arka planda izlenir; ham radar kaydı silinmez."
    )
    return payload


def _match_historical(item, regions):
    if not _historical_candidate(item):
        return None

    for region_key, region_data in regions.items():
        if not isinstance(region_data, dict) or region_data.get("durum") != "ok":
            continue
        region_label = region_data.get("bolge") or region_key
        if not provenance._region_compatible(item, region_label):
            continue
        examples = region_data.get("secili_genis_sekil_ornekleri") or []
        if not isinstance(examples, list):
            continue
        max_m = float(
            region_data.get("nihai_rapor_yaklasik_esleme_esigi_m")
            or DEFAULT_MATCH_METERS
        )
        min_similarity = float(
            region_data.get("nihai_rapor_yaklasik_min_alan_benzerligi")
            or DEFAULT_MIN_AREA_SIMILARITY
        )
        match = provenance._match_example(item, examples, max_m, min_similarity)
        if match is None:
            continue
        example, distance, similarity = match
        background = _background_from_match(
            item,
            example,
            region_key,
            region_label,
            distance,
            similarity,
        )
        if background is not None:
            return background
    return None


def apply_historical_backlog_guard(payload):
    if not isinstance(payload, dict):
        return payload, [], []
    report_date = payload.get("rapor_tarihi")
    regions = provenance._load_shape_regions(report_date)
    if not regions:
        return payload, [], []

    operational = []
    separated = []
    manual_overrides = []
    additions = []

    for item in payload.get("saha_adaylari") or []:
        if not isinstance(item, dict):
            continue
        if base._manual_repeat(item):
            manual_overrides.append(dict(item))
            operational.append(dict(item))
            continue
        matched = _match_historical(item, regions)
        if matched is None:
            operational.append(dict(item))
            continue
        separated.append(dict(item))
        additions.append(matched)

    backgrounds = base._dedupe_candidates(
        list(payload.get("arka_plan_genis_yuzey_hareketleri") or []) + additions
    )
    payload["saha_adaylari"] = operational
    payload["arka_plan_genis_yuzey_hareketleri"] = backgrounds

    rule = payload.get("arka_plan_kurali")
    if not isinstance(rule, dict):
        rule = {}
        payload["arka_plan_kurali"] = rule
    rule["tarihsel_tasima"] = {
        "aktif": True,
        "yalniz_yeni_goruntu_false": True,
        "minimum_bekleme_gun": MIN_WAIT_DAYS,
        "minimum_alan_m2": base.BACKGROUND_MIN_M2,
        "maksimum_esleme_m": DEFAULT_MATCH_METERS,
        "minimum_alan_benzerligi": DEFAULT_MIN_AREA_SIMILARITY,
        "kanit": "aynı-gün shape_false_positive_audit üretim seçimi",
        "aciklama": (
            "Yalnız tarihsel/gecikmiş geniş görev, güncel düşük-kompaktlık geometriyle "
            "güvenli provenans sınırında yeniden eşleşirse arka plana ayrılır. Taze görev "
            "ve insanın TEKRAR_GIT kararı korunur."
        ),
    }
    payload["ozet"] = base._summary_after_filter(
        payload.get("ozet"), operational, backgrounds
    )
    return payload, separated, manual_overrides


def _self_check():
    old_wide = {
        "bolge": "Çeşme merkez · Alaçatı · Ilıca",
        "enlem": 38.253128,
        "boylam": 26.424511,
        "alan_m2": 204_420,
        "yeni_goruntu": False,
        "gecikmis": True,
        "bekleme_gun": 5,
        "saha_durumu": "KONTROLE_GIT",
    }
    fresh_wide = {**old_wide, "yeni_goruntu": True, "bekleme_gun": 0, "gecikmis": False}
    small_old = {**old_wide, "alan_m2": 9_000}
    manual = {**old_wide, "saha_durumu": "TEKRAR_GIT"}
    assert _historical_candidate(old_wide)
    assert not _historical_candidate(fresh_wide)
    assert not _historical_candidate(small_old)
    assert not _historical_candidate(manual)

    example = {
        "enlem": 38.253128,
        "boylam": 26.424511,
        "alan_m2": 207_420,
        "kompaktlik": 0.072,
        "dusuk_kompaktlik": True,
    }
    regions = {
        "cesme": {
            "durum": "ok",
            "bolge": "Çeşme merkez · Alaçatı · Ilıca",
            "nihai_rapor_yaklasik_esleme_esigi_m": 25,
            "nihai_rapor_yaklasik_min_alan_benzerligi": 0.60,
            "nihai_rapor_yaklasik_eslesen_geometri": 0,
            "secili_genis_sekil_ornekleri": [example],
        }
    }
    # Kritik regresyon: nihai yaklaşık eşleşme sayısı sıfır olsa bile tarihsel
    # görev, güncel üretim-seçimi geometrisiyle yeniden doğrulanabilmeli.
    matched = _match_historical(old_wide, regions)
    assert matched is not None
    assert matched.get("kanit_kaynagi") == SOURCE_NAME
    assert _match_historical(fresh_wide, regions) is None


def run():
    _self_check()
    if not REPORT_JSON.exists():
        print("latest_report.json yok; tarihsel geniş backlog koruması değişiklik yapmadı.")
        return
    try:
        payload = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        print("latest_report.json okunamadı; tarihsel geniş backlog koruması değişiklik yapmadı.")
        return

    payload, separated, manual_overrides = apply_historical_backlog_guard(payload)
    REPORT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    backgrounds = payload.get("arka_plan_genis_yuzey_hareketleri") or []
    base.annotate_markdown(payload, backgrounds)
    base.write_review(
        payload.get("rapor_tarihi"),
        backgrounds,
        separated,
        manual_overrides,
    )
    print(
        "Tarihsel geniş backlog koruması: "
        f"{len(separated)} eski operasyon kaydı arka plana ayrıldı; "
        f"{len(backgrounds)} toplam geniş-yüzey arka plan kaydı; "
        f"{len(manual_overrides)} manuel tekrar korundu."
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Tarihsel geniş backlog öz testi başarılı.")
        return
    run()


if __name__ == "__main__":
    main()
