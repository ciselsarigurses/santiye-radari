"""Geniş yüzey filtresindeki yaklaşık-geometri provenans boşluğunu kapatır.

Gülbahçe kapsaması ana Uzunkuyu Sentinel kutusuna eklendikten sonra aynı gerçek
kümeler birkaç metre farklı temsil koordinatıyla raporlanabiliyor. Bağımsız şekil
denetimi bu durumu ``nihai_rapor_yaklasik_eslesen_geometri`` olarak doğruluyor;
ancak ana geniş-yüzey koruması yalnız tam eşleşen nihai örnekleri kullandığı için
Uzunkuyu/Gülbahçe tarafındaki ölçülmüş düşük-kompaktlık geniş hareketler operasyon
listesinde kalabiliyordu.

Bu sarmalayıcı önce mevcut geniş-yüzey korumasını aynen çalıştırır, sonra yalnız
şekil denetiminin kendi güvenli yaklaşık-provenans sınırları içinde (varsayılan
25 m ve alan benzerliği >= 0.60) eşleşen 10.000 m² üstü düşük-kompaktlık hareketleri
arka plana ayırır. 250 m² ana alarm eşiğini, 150-249 m² Mikro Şantiye katmanını,
SQLite radar hafızasını veya insanın TEKRAR_GIT kararını değiştirmez.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import wide_surface_background_guard as base


ROOT = Path(__file__).resolve().parent
REPORT_JSON = ROOT / "latest_report.json"
SHAPE_AUDIT_JSON = ROOT / "shape_false_positive_audit.json"

DEFAULT_MATCH_METERS = 25.0
DEFAULT_MIN_AREA_SIMILARITY = 0.60
SOURCE_NAME = "shape_false_positive_audit_yaklasik"


def _region_compatible(item, region_label):
    item_label = str(item.get("bolge") or "").strip()
    expected = str(region_label or "").strip()
    if not item_label or not expected:
        return False
    if item_label == expected:
        return True
    # Gülbahçe ana bbox'a eklendiğinde daha önce açılmış görevler eski etiketi
    # taşıyabiliyor. Yalnız aynı Uzunkuyu/Germiyan/Ildır çekirdeği için kabul et.
    if expected.endswith(" · Gülbahçe") and item_label == expected[: -len(" · Gülbahçe")]:
        return True
    return False


def _eligible_example(raw):
    if not isinstance(raw, dict):
        return False
    area = max(base._number(raw.get("alan_m2"), 0.0) or 0.0, 0.0)
    compactness = base._number(raw.get("kompaktlik"))
    return bool(
        area > base.BACKGROUND_MIN_M2
        and raw.get("dusuk_kompaktlik") is True
        and compactness is not None
        and compactness <= base.LOW_COMPACTNESS_MAX
        and base._point(raw) is not None
    )


def _match_example(item, examples, max_m, min_similarity):
    if not isinstance(item, dict) or base._manual_repeat(item):
        return None
    area = max(base._number(item.get("alan_m2"), 0.0) or 0.0, 0.0)
    point = base._point(item)
    if area <= base.BACKGROUND_MIN_M2 or point is None:
        return None

    best = None
    for example in examples:
        if not _eligible_example(example):
            continue
        distance = base._distance_m(point, base._point(example))
        if distance > max_m:
            continue
        similarity = base._area_similarity(area, example.get("alan_m2"))
        if similarity < min_similarity:
            continue
        score = (distance, -similarity)
        if best is None or score < best[0]:
            best = (score, example, distance, similarity)
    if best is None:
        return None
    return best[1], best[2], best[3]


def _background_from_match(item, example, region_key, region_label, distance, similarity):
    compactness = float(base._number(example.get("kompaktlik"), 0.0) or 0.0)
    normalized = {
        **item,
        "genis_kompaktlik": compactness,
        "genis_geometri_riski": True,
        "sinyal": "Şekil denetiminde yaklaşık provenansla doğrulanan geniş düşük-kompaktlık yüzey hareketi",
        "kanit_kaynagi": SOURCE_NAME,
    }
    payload = base._candidate_payload(normalized, region_key, region_label)
    if payload is None:
        return None
    payload["geometri_esleme_mesafe_m"] = round(float(distance), 1)
    payload["geometri_alan_benzerligi"] = round(float(similarity), 3)
    payload["geometri_kaynagi_enlem"] = round(float(example.get("enlem")), 6)
    payload["geometri_kaynagi_boylam"] = round(float(example.get("boylam")), 6)
    payload["neden"] = (
        "10.000 m² üstünde ve bağımsız şekil denetiminde düşük-kompaktlık riski ölçüldü. "
        f"Nihai rapor koordinatı aynı geometriye {distance:.1f} m ve alan benzerliği "
        f"{similarity:.2f} ile eşleşiyor; bu, şekil denetiminin kendi yaklaşık-provenans "
        "sınırları içindedir. Bu nedenle geniş arazi/tarım/toprak/doğal yüzey hareketi "
        "olarak arka planda izlenir; ham radar kaydı silinmez."
    )
    return payload


def _load_shape_regions(report_date):
    if not SHAPE_AUDIT_JSON.exists():
        return {}
    try:
        audit = json.loads(SHAPE_AUDIT_JSON.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(audit, dict) or str(audit.get("rapor_tarihi") or "") != str(report_date or ""):
        return {}
    regions = audit.get("bolgeler") or {}
    return regions if isinstance(regions, dict) else {}


def _persisted_gap_backgrounds(payload):
    if not isinstance(payload, dict):
        return []
    result = []
    for raw in payload.get("arka_plan_genis_yuzey_hareketleri") or []:
        if isinstance(raw, dict) and raw.get("kanit_kaynagi") == SOURCE_NAME:
            result.append(dict(raw))
    return result


def _apply_gap_layer(payload, persisted):
    if not isinstance(payload, dict):
        return payload, [], []
    report_date = payload.get("rapor_tarihi")
    regions = _load_shape_regions(report_date)
    operational = []
    separated = []
    manual_overrides = []

    for item in payload.get("saha_adaylari") or []:
        if not isinstance(item, dict):
            continue
        if base._manual_repeat(item):
            manual_overrides.append(dict(item))
            operational.append(dict(item))
            continue

        matched_background = None
        for region_key, region_data in regions.items():
            if not isinstance(region_data, dict) or region_data.get("durum") != "ok":
                continue
            approx_count = int(region_data.get("nihai_rapor_yaklasik_eslesen_geometri") or 0)
            if approx_count <= 0:
                continue
            region_label = region_data.get("bolge") or region_key
            if not _region_compatible(item, region_label):
                continue
            max_m = float(region_data.get("nihai_rapor_yaklasik_esleme_esigi_m") or DEFAULT_MATCH_METERS)
            min_similarity = float(
                region_data.get("nihai_rapor_yaklasik_min_alan_benzerligi")
                or DEFAULT_MIN_AREA_SIMILARITY
            )
            examples = region_data.get("secili_genis_sekil_ornekleri") or []
            if not isinstance(examples, list):
                continue
            match = _match_example(item, examples, max_m, min_similarity)
            if match is None:
                continue
            example, distance, similarity = match
            matched_background = _background_from_match(
                item,
                example,
                region_key,
                region_label,
                distance,
                similarity,
            )
            break

        if matched_background is None:
            operational.append(dict(item))
        else:
            separated.append(dict(item))
            persisted.append(matched_background)

    backgrounds = base._dedupe_candidates(
        list(payload.get("arka_plan_genis_yuzey_hareketleri") or []) + persisted
    )
    payload["saha_adaylari"] = operational
    payload["arka_plan_genis_yuzey_hareketleri"] = backgrounds
    rule = payload.get("arka_plan_kurali")
    if not isinstance(rule, dict):
        rule = {}
        payload["arka_plan_kurali"] = rule
    rule["yaklasik_provenans"] = {
        "aktif": True,
        "maksimum_esleme_m": DEFAULT_MATCH_METERS,
        "minimum_alan_benzerligi": DEFAULT_MIN_AREA_SIMILARITY,
        "kanit": "shape_false_positive_audit üretim seçimi + nihai yaklaşık provenans doğrulaması",
        "aciklama": (
            "BBox/yeniden-pikselleştirme nedeniyle birkaç metre kayan düşük-kompaktlık geniş "
            "kümeler yalnız şekil denetiminin kendi 25 m / 0.60 güven sınırında arka plana ayrılır."
        ),
    }
    payload["ozet"] = base._summary_after_filter(
        payload.get("ozet"), operational, backgrounds
    )
    return payload, separated, manual_overrides


def _self_check():
    example = {
        "enlem": 38.308663,
        "boylam": 26.462992,
        "alan_m2": 120199,
        "kompaktlik": 0.059,
        "dusuk_kompaktlik": True,
    }
    report = {
        "bolge": "Uzunkuyu · Germiyan · Ildır · Gülbahçe",
        "enlem": 38.308663,
        "boylam": 26.462996,
        "alan_m2": 120241,
        "saha_durumu": "KONTROLE_GIT",
    }
    assert _region_compatible(report, "Uzunkuyu · Germiyan · Ildır · Gülbahçe")
    match = _match_example(report, [example], 25.0, 0.60)
    assert match is not None and match[1] < 1.0 and match[2] > 0.99
    assert _match_example({**report, "enlem": 38.308908}, [example], 25.0, 0.60) is None
    assert _match_example({**report, "alan_m2": 50_000}, [example], 25.0, 0.60) is None
    assert _match_example({**report, "saha_durumu": "TEKRAR_GIT"}, [example], 25.0, 0.60) is None
    assert not _eligible_example({**example, "kompaktlik": 0.22})


def run():
    _self_check()
    if not REPORT_JSON.exists():
        print("latest_report.json yok; yaklaşık provenans katmanı değişiklik yapmadı.")
        return

    try:
        before = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        before = {}
    persisted = _persisted_gap_backgrounds(before)

    report_date = before.get("rapor_tarihi") if isinstance(before, dict) else None
    if not report_date:
        print("Rapor tarihi bulunamadı; yaklaşık provenans katmanı değişiklik yapmadı.")
        return

    # Önce ana korumayı güncel rapor üzerinde çalıştır.
    backgrounds = base.load_background_candidates(report_date)
    payload, _, _ = base.annotate_json(backgrounds)
    if not isinstance(payload, dict):
        print("Ana geniş-yüzey katmanı raporu okuyamadı.")
        return

    payload, separated, manual_overrides = _apply_gap_layer(payload, persisted)
    REPORT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    final_backgrounds = payload.get("arka_plan_genis_yuzey_hareketleri") or []
    base.annotate_markdown(payload, final_backgrounds)
    base.write_review(report_date, final_backgrounds, separated, manual_overrides)
    print(
        "Geniş yüzey yaklaşık-provenans koruması: "
        f"{len(final_backgrounds)} toplam arka-plan hareketi; "
        f"{len(separated)} ek operasyon kaydı ayrıldı; "
        f"{len(manual_overrides)} manuel tekrar korundu."
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Geniş yüzey yaklaşık-provenans öz testi başarılı.")
        return
    run()


if __name__ == "__main__":
    main()
