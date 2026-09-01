"""Diyagonal 4-komşu parsel yan-kümeleri için yalnız-denetim karşılaştırması.

Üretim alarmını, 250 m² eşiğini veya 24 aday tavanını değiştirmez. Mevcut
``rebalance_satellite_candidates`` katmanının güvenli biçimde çıkardığı
``DIYAGONAL_YAN_KUME`` adaylarını sayar ve bir sonraki Sentinel sahnesinde yan-küme
kotasının 1 yerine 2 olması durumunda aynı 24 kayıt içinde hangi adayın yer
değiştireceğini ölçer. Böylece koordinat hassasiyeti için ikinci diyagonal parsel
yerinin gerçekten daha güçlü kanıt getirip getirmediği, saha kuyruğunu büyütmeden
ölçülebilir.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import rebalance_satellite_candidates as rebalance
import satellite
from daily_report import ISTANBUL, REPORT_REGIONS


AUDIT_FILE = Path("diagonal_sidecar_audit.json")
SIMULATED_SIDECAR_QUOTA = 2


def _key(item):
    return rebalance._candidate_key(item)


def _detail(item):
    if not isinstance(item, dict):
        return None
    return {
        "mahalle": item.get("mahalle"),
        "enlem": item.get("enlem"),
        "boylam": item.get("boylam"),
        "alan_m2": round(rebalance._area(item)),
        "guclu_sinyal_orani": round(rebalance._signal_strength(item), 4),
        "geometri_kaynagi": item.get("geometri_kaynagi"),
    }


def _selected_difference(before, after):
    before_by_key = {
        _key(item): item for item in before if _key(item) is not None
    }
    after_by_key = {
        _key(item): item for item in after if _key(item) is not None
    }
    added = [after_by_key[key] for key in after_by_key.keys() - before_by_key.keys()]
    removed = [before_by_key[key] for key in before_by_key.keys() - after_by_key.keys()]
    added.sort(key=lambda item: (-rebalance._signal_strength(item), rebalance._area(item)))
    removed.sort(key=lambda item: (rebalance._signal_strength(item), -rebalance._area(item)))
    return added, removed


def _sidecars(items):
    return [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("geometri_kaynagi") == rebalance.DIAGONAL_SIDECAR_TAG
    ]


def _self_check():
    def item(index, area, strength=0.0, sidecar=False):
        value = {
            "enlem": 38.20 + index * 0.001,
            "boylam": 26.30 + index * 0.001,
            "alan_m2": area,
            rebalance.STRONG_SIGNAL_FIELD: strength,
        }
        if sidecar:
            value["geometri_kaynagi"] = rebalance.DIAGONAL_SIDECAR_TAG
        return value

    candidates = []
    candidates.extend(item(i, 300 + i * 50, 0.8) for i in range(6))
    candidates.extend(item(20 + i, 1000 + i * 500, 0.30 + i * 0.01) for i in range(10))
    candidates.extend(item(50 + i, 12000 + i * 1000, 0.10) for i in range(20))
    candidates.extend(
        [
            item(90, 1200, 0.91, sidecar=True),
            item(91, 1800, 0.88, sidecar=True),
        ]
    )

    baseline = rebalance._balanced_select(candidates)
    simulated = rebalance._balanced_select(
        candidates,
        diagonal_sidecar_quota=SIMULATED_SIDECAR_QUOTA,
    )
    assert len(baseline) == satellite.HOTSPOT_LIMIT
    assert len(simulated) == satellite.HOTSPOT_LIMIT
    assert len(_sidecars(baseline)) == 1
    assert len(_sidecars(simulated)) == 2
    added, removed = _selected_difference(baseline, simulated)
    assert len(added) == 1 and len(removed) == 1
    assert added[0].get("geometri_kaynagi") == rebalance.DIAGONAL_SIDECAR_TAG
    assert rebalance._bucket_counts(baseline) == rebalance._bucket_counts(simulated)


def audit():
    _self_check()
    now = __import__("datetime").datetime.now(ISTANBUL)
    regions = {}

    for region_key in REPORT_REGIONS:
        record = {
            "bolge": satellite.REGIONS[region_key]["label"],
            "durum": "ok",
        }
        try:
            pair = satellite.sentinel_pair(region_key)
            _, latest = pair
            record["latest_item"] = latest.get("id")
            raw_result = rebalance._uncapped_analysis(region_key, pair)
            candidates = [
                item for item in raw_result.get("hotspots", [])
                if isinstance(item, dict)
            ]
            sidecar_pool = _sidecars(candidates)
            baseline = rebalance._balanced_select(candidates)
            simulated = rebalance._balanced_select(
                candidates,
                diagonal_sidecar_quota=SIMULATED_SIDECAR_QUOTA,
            )
            baseline_sidecars = _sidecars(baseline)
            simulated_sidecars = _sidecars(simulated)
            added, removed = _selected_difference(baseline, simulated)

            ranked_sidecars = sorted(
                sidecar_pool,
                key=lambda item: (
                    -rebalance._signal_strength(item),
                    rebalance._area(item),
                    float(item.get("enlem") or 0),
                    float(item.get("boylam") or 0),
                ),
            )
            record.update(
                {
                    "ham_aday": len(candidates),
                    "guvenli_diyagonal_yan_kume_havuzu": len(sidecar_pool),
                    "erken_parsel_yan_kume": sum(
                        rebalance._area(item) <= rebalance.EARLY_PARCEL_MAX_M2
                        for item in sidecar_pool
                    ),
                    "mevcut_kota": rebalance.DIAGONAL_SIDECAR_QUOTA,
                    "mevcut_secili_yan_kume": len(baseline_sidecars),
                    "simulasyon_kota": SIMULATED_SIDECAR_QUOTA,
                    "simulasyon_secili_yan_kume": len(simulated_sidecars),
                    "toplam_aday_mevcut": len(baseline),
                    "toplam_aday_simulasyon": len(simulated),
                    "olcek_dagilimi_mevcut": rebalance._bucket_counts(baseline),
                    "olcek_dagilimi_simulasyon": rebalance._bucket_counts(simulated),
                    "eklenen": [_detail(item) for item in added],
                    "cikarilan": [_detail(item) for item in removed],
                    "en_guclu_yan_kumeler": [
                        _detail(item) for item in ranked_sidecars[:5]
                    ],
                }
            )

            assert len(baseline) == len(simulated), (
                "Diyagonal kota simülasyonu toplam aday sayısını değiştirdi."
            )
            assert rebalance._bucket_counts(baseline) == rebalance._bucket_counts(simulated), (
                "Diyagonal kota simülasyonu ölçek dağılımını değiştirdi."
            )
            assert len(added) == len(removed), (
                "Diyagonal kota simülasyonu bire bir takas üretmedi."
            )
        except Exception as exc:
            record["durum"] = "denetim_hatasi"
            record["hata"] = f"{type(exc).__name__}: {exc}"
        regions[region_key] = record

    payload = {
        "rapor_tarihi": now.strftime("%Y-%m-%d"),
        "olusturma": now.strftime("%Y-%m-%d %H:%M %Z"),
        "amac": (
            "Geniş 8-komşu kümelerden güvenli biçimde ayrıştırılan 800-10.000 m² "
            "diyagonal yan-kümelerde ikinci kota yerinin koordinat değerini alarm "
            "sayısını artırmadan ölçmek; bu dosya alarm veya saha görevi üretmez."
        ),
        "bolgeler": regions,
    }
    AUDIT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print(
            "Diyagonal yan-küme kota denetimi öz testi başarılı: 24 aday ve ölçek "
            "dağılımı sabit kalırken ikinci yan-küme yalnız bire bir takasla ölçülüyor."
        )
        return

    payload = audit()
    summaries = []
    for key, item in payload["bolgeler"].items():
        if item.get("durum") != "ok":
            summaries.append(f"{key}: {item.get('durum')}")
            continue
        summaries.append(
            f"{key}: yan-küme havuzu={item.get('guvenli_diyagonal_yan_kume_havuzu', 0)}, "
            f"mevcut seçili={item.get('mevcut_secili_yan_kume', 0)}, "
            f"kota2 seçili={item.get('simulasyon_secili_yan_kume', 0)}, "
            f"takas={len(item.get('eklenen') or [])}"
        )
    print("Diyagonal yan-küme kota denetimi: " + " | ".join(summaries))


if __name__ == "__main__":
    main()
