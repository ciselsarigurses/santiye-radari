"""Diyagonal parsel yan-kümelerini yalnız ölçülebilir biçimde daha iyi ise terfi ettir.

Bu katman ana Sentinel eşiklerini, 250 m² alt sınırını, 24 aday tavanını veya
6 şantiye-ölçeği kotasını değiştirmez. ``rebalance_satellite_candidates`` tarafından
üretilen 0, 1 ve 2 ``DIYAGONAL_YAN_KUME`` içeren karşı-olgusal seçimleri aynı sahnede
karşılaştırır. İlk yan-küme de ikinci yan-küme de yalnız bire bir yer değiştirdiği
normal şantiye-ölçeği adayından daha yüksek ``guclu_sinyal_orani`` taşıyorsa seçilir.
Böylece yalnız geometri kurtarma kotası var diye 0 güçlü-sinyalli bir yan-küme daha
güçlü normal hafriyat adayını saha listesinden çıkaramaz.

Yeni politika mevcut Sentinel sahnesine geriye dönük uygulanmaz. İlk çalışmada sahne
zaten eskiyse o sahne ``grandfathered`` olarak kaydedilir; ilk sonraki Sentinel ürününde
politika devreye girer. Aynı yeni sahne sonraki günlerde tekrar işlense de aynı karar
yeniden üretilebilir. Böylece kod değişikliği tek başına saha kuyruğunu şişirmez.

Normal çalışmada mevcut ``diagonal_sidecar_audit.json`` dosyasını da üretir; bu nedenle
aynı ham Sentinel analizi yalnız bir kez yapılır. ``--check-only`` ağ veya veritabanı
kullanmadan ilk ve ikinci yan-küme terfi/ret regresyonlarını sınar.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import rebalance_satellite_candidates as rebalance
import satellite
from daily_report import ISTANBUL, REPORT_REGIONS, build_daily_report, ensure_daily_schema
from scanner import connect


POLICY_VERSION = "adaptive-diagonal-sidecar-v2-guard-first-next-scene"
NO_SIDECAR_QUOTA = 0
BASELINE_SIDECAR_QUOTA = 1
CANDIDATE_SIDECAR_QUOTA = 2
AUDIT_FILE = Path("diagonal_sidecar_audit.json")


def _key(item):
    return rebalance._candidate_key(item)


def _sidecars(items):
    return [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("geometri_kaynagi") == rebalance.DIAGONAL_SIDECAR_TAG
    ]


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
    before_by_key = {_key(item): item for item in before if _key(item) is not None}
    after_by_key = {_key(item): item for item in after if _key(item) is not None}
    added = [after_by_key[key] for key in after_by_key.keys() - before_by_key.keys()]
    removed = [before_by_key[key] for key in before_by_key.keys() - after_by_key.keys()]
    added.sort(key=lambda item: (-rebalance._signal_strength(item), rebalance._area(item)))
    removed.sort(key=lambda item: (rebalance._signal_strength(item), -rebalance._area(item)))
    return added, removed


def _adaptive_choice(baseline, expanded, ordinal):
    """Aynı bütçede yalnız daha güçlü tek bir yan-küme takasını kabul et."""
    prefix = str(ordinal)
    if len(baseline) != len(expanded):
        return baseline, f"{prefix}_butce_farki", [], []
    if rebalance._bucket_counts(baseline) != rebalance._bucket_counts(expanded):
        return baseline, f"{prefix}_olcek_dagilimi_farki", [], []

    added, removed = _selected_difference(baseline, expanded)
    if not added and not removed:
        return baseline, f"{prefix}_yan_kume_yok", added, removed
    if len(added) != 1 or len(removed) != 1:
        return baseline, f"{prefix}_bire_bir_takas_degil", added, removed

    challenger = added[0]
    displaced = removed[0]
    if challenger.get("geometri_kaynagi") != rebalance.DIAGONAL_SIDECAR_TAG:
        return baseline, f"{prefix}_eklenen_yan_kume_degil", added, removed
    if displaced.get("geometri_kaynagi") == rebalance.DIAGONAL_SIDECAR_TAG:
        return baseline, f"{prefix}_yan_kume_yan_kumeyi_degistiriyor", added, removed

    challenger_strength = rebalance._signal_strength(challenger)
    displaced_strength = rebalance._signal_strength(displaced)
    if challenger_strength <= displaced_strength:
        return baseline, f"{prefix}_yan_kume_daha_guclu_degil", added, removed

    return expanded, f"{prefix}_yan_kume_terfi", added, removed


def _ensure_state_table(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS uydu_adaptive_yan_kume_surumu (
        bolge TEXT PRIMARY KEY,
        son_item TEXT NOT NULL,
        surum TEXT NOT NULL,
        karar TEXT NOT NULL,
        guncelleme TEXT NOT NULL)"""
    )


def _state_decision(connection, region_key, latest_item, new_image):
    """Mevcut sahneyi retrofit etme; sonraki sahnelerde kararı tekrarlanabilir kıl."""
    state = connection.execute(
        """SELECT son_item,surum,karar FROM uydu_adaptive_yan_kume_surumu
        WHERE bolge=? LIMIT 1""",
        (region_key,),
    ).fetchone()

    if state and str(state[0]) == str(latest_item):
        if str(state[1]) != POLICY_VERSION:
            return False, "mevcut_sahne_korundu"
        if str(state[2]) == "mevcut_sahne_korundu":
            return False, "mevcut_sahne_korundu"
        return True, "ayni_yeni_sahne_tekrar"

    if state and str(state[0]) != str(latest_item):
        return True, "yeni_sahne"

    if bool(new_image):
        return True, "ilk_calismada_yeni_sahne"
    return False, "mevcut_sahne_korundu"


def _store_state(connection, region_key, latest_item, decision):
    connection.execute(
        """INSERT INTO uydu_adaptive_yan_kume_surumu
        (bolge,son_item,surum,karar,guncelleme)
        VALUES(?,?,?,?,?)
        ON CONFLICT(bolge) DO UPDATE SET
        son_item=excluded.son_item,surum=excluded.surum,
        karar=excluded.karar,guncelleme=excluded.guncelleme""",
        (
            region_key,
            latest_item,
            POLICY_VERSION,
            decision,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


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

    base = []
    base.extend(item(i, 300 + i * 50, 0.8) for i in range(6))
    base.extend(
        [
            item(20, 1000, 0.70),
            item(21, 1500, 0.60),
            item(22, 1900, 0.50),
            item(23, 4000, 0.40),
            item(24, 5000, 0.30),
            item(25, 6000, 0.20),
            item(26, 7000, 0.10),
        ]
    )
    base.extend(item(50 + i, 12000 + i * 1000, 0.10) for i in range(20))

    promote_pool = list(base) + [
        item(90, 3500, 0.90, sidecar=True),
        item(91, 5400, 0.35, sidecar=True),
    ]
    no_sidecar = rebalance._balanced_select(
        promote_pool, diagonal_sidecar_quota=NO_SIDECAR_QUOTA
    )
    baseline_raw = rebalance._balanced_select(
        promote_pool, diagonal_sidecar_quota=BASELINE_SIDECAR_QUOTA
    )
    baseline, first_reason, first_added, first_removed = _adaptive_choice(
        no_sidecar, baseline_raw, "ilk"
    )
    assert first_reason == "ilk_yan_kume_terfi", (
        first_reason,
        first_added,
        first_removed,
    )
    assert len(_sidecars(no_sidecar)) == 0
    assert len(_sidecars(baseline)) == 1
    assert len(first_added) == len(first_removed) == 1
    assert rebalance._signal_strength(first_added[0]) > rebalance._signal_strength(
        first_removed[0]
    )

    expanded = rebalance._balanced_select(
        promote_pool, diagonal_sidecar_quota=CANDIDATE_SIDECAR_QUOTA
    )
    chosen, second_reason, added, removed = _adaptive_choice(
        baseline, expanded, "ikinci"
    )
    assert second_reason == "ikinci_yan_kume_terfi", (
        second_reason,
        added,
        removed,
    )
    assert len(_sidecars(chosen)) == 2
    assert len(added) == len(removed) == 1
    assert rebalance._signal_strength(added[0]) > rebalance._signal_strength(removed[0])
    assert len(chosen) == satellite.HOTSPOT_LIMIT
    assert rebalance._bucket_counts(chosen) == rebalance._bucket_counts(no_sidecar)

    first_reject_pool = list(base) + [item(92, 3500, 0.0, sidecar=True)]
    first_reject_no = rebalance._balanced_select(
        first_reject_pool, diagonal_sidecar_quota=NO_SIDECAR_QUOTA
    )
    first_reject_raw = rebalance._balanced_select(
        first_reject_pool, diagonal_sidecar_quota=BASELINE_SIDECAR_QUOTA
    )
    first_reject, first_reject_reason, first_reject_added, first_reject_removed = (
        _adaptive_choice(first_reject_no, first_reject_raw, "ilk")
    )
    assert first_reject_reason == "ilk_yan_kume_daha_guclu_degil", (
        first_reject_reason,
        first_reject_added,
        first_reject_removed,
    )
    assert {_key(item) for item in first_reject} == {
        _key(item) for item in first_reject_no
    }
    assert len(_sidecars(first_reject)) == 0

    second_reject_pool = list(base) + [
        item(93, 3500, 0.90, sidecar=True),
        item(94, 5400, 0.25, sidecar=True),
    ]
    second_no = rebalance._balanced_select(
        second_reject_pool, diagonal_sidecar_quota=NO_SIDECAR_QUOTA
    )
    second_one_raw = rebalance._balanced_select(
        second_reject_pool, diagonal_sidecar_quota=BASELINE_SIDECAR_QUOTA
    )
    second_one, second_first_reason, _, _ = _adaptive_choice(
        second_no, second_one_raw, "ilk"
    )
    assert second_first_reason == "ilk_yan_kume_terfi"
    second_two = rebalance._balanced_select(
        second_reject_pool, diagonal_sidecar_quota=CANDIDATE_SIDECAR_QUOTA
    )
    second_reject, second_reject_reason, second_reject_added, second_reject_removed = (
        _adaptive_choice(second_one, second_two, "ikinci")
    )
    assert second_reject_reason == "ikinci_yan_kume_daha_guclu_degil", (
        second_reject_reason,
        second_reject_added,
        second_reject_removed,
    )
    assert {_key(item) for item in second_reject} == {
        _key(item) for item in second_one
    }
    assert len(_sidecars(second_reject)) == 1


def run_guard():
    ensure_daily_schema()
    _self_check()
    now = datetime.now(ISTANBUL)
    report_date = now.strftime("%Y-%m-%d")
    records = {}
    applied = []
    skipped = []
    errors = []

    with connect() as connection:
        _ensure_state_table(connection)
        for region_key in REPORT_REGIONS:
            record = {
                "bolge": satellite.REGIONS[region_key]["label"],
                "durum": "ok",
            }
            try:
                row = connection.execute(
                    """SELECT son_item,hareket_json,hata,yeni_goruntu
                    FROM gunluk_uydu_raporlari
                    WHERE rapor_tarihi=? AND bolge=? LIMIT 1""",
                    (report_date, region_key),
                ).fetchone()
                if not row or row[2] or not row[0]:
                    record["durum"] = "uygun_uydu_raporu_yok"
                    skipped.append(region_key)
                    records[region_key] = record
                    continue

                latest_item = str(row[0])
                pair = satellite.sentinel_pair(region_key)
                if str(pair[1].get("id") or "") != latest_item:
                    record["durum"] = "sentinel_sahne_eslesmedi"
                    skipped.append(region_key)
                    records[region_key] = record
                    continue

                raw_result = rebalance._uncapped_analysis(region_key, pair)
                candidates = [
                    item
                    for item in raw_result.get("hotspots", [])
                    if isinstance(item, dict)
                ]
                no_sidecar = rebalance._balanced_select(
                    candidates,
                    diagonal_sidecar_quota=NO_SIDECAR_QUOTA,
                )
                baseline_raw = rebalance._balanced_select(
                    candidates,
                    diagonal_sidecar_quota=BASELINE_SIDECAR_QUOTA,
                )
                baseline, first_reason, first_added, first_removed = _adaptive_choice(
                    no_sidecar, baseline_raw, "ilk"
                )
                expanded = rebalance._balanced_select(
                    candidates,
                    diagonal_sidecar_quota=CANDIDATE_SIDECAR_QUOTA,
                )

                if first_reason == "ilk_yan_kume_terfi":
                    chosen, second_reason, second_added, second_removed = _adaptive_choice(
                        baseline, expanded, "ikinci"
                    )
                    adaptive_reason = second_reason
                else:
                    chosen = baseline
                    second_reason = "ilk_yan_kume_gecmedi"
                    second_added, second_removed = [], []
                    adaptive_reason = first_reason

                final_added, final_removed = _selected_difference(no_sidecar, chosen)
                allow_apply, scene_reason = _state_decision(
                    connection, region_key, latest_item, bool(row[3])
                )

                applied_decision = "mevcut_sahne_korundu"
                if allow_apply:
                    connection.execute(
                        """UPDATE gunluk_uydu_raporlari SET hareket_json=?
                        WHERE rapor_tarihi=? AND bolge=? AND son_item=?""",
                        (
                            json.dumps(chosen, ensure_ascii=False),
                            report_date,
                            region_key,
                            latest_item,
                        ),
                    )
                    applied_decision = adaptive_reason
                    applied.append((region_key, adaptive_reason))
                else:
                    skipped.append(region_key)

                _store_state(connection, region_key, latest_item, applied_decision)

                sidecar_pool = _sidecars(candidates)
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
                        "latest_item": latest_item,
                        "ham_aday": len(candidates),
                        "guvenli_diyagonal_yan_kume_havuzu": len(sidecar_pool),
                        "erken_parsel_yan_kume": sum(
                            rebalance._area(item) <= rebalance.EARLY_PARCEL_MAX_M2
                            for item in sidecar_pool
                        ),
                        "mevcut_kota": BASELINE_SIDECAR_QUOTA,
                        "ham_mevcut_secili_yan_kume": len(_sidecars(baseline_raw)),
                        "mevcut_secili_yan_kume": len(_sidecars(baseline)),
                        "simulasyon_kota": CANDIDATE_SIDECAR_QUOTA,
                        "simulasyon_secili_yan_kume": len(_sidecars(expanded)),
                        "son_secili_yan_kume": len(_sidecars(chosen)),
                        "toplam_aday_mevcut": len(baseline),
                        "toplam_aday_simulasyon": len(expanded),
                        "toplam_aday_son": len(chosen),
                        "olcek_dagilimi_mevcut": rebalance._bucket_counts(baseline),
                        "olcek_dagilimi_simulasyon": rebalance._bucket_counts(expanded),
                        "olcek_dagilimi_son": rebalance._bucket_counts(chosen),
                        "ilk_yan_kume_karari": first_reason,
                        "ikinci_yan_kume_karari": second_reason,
                        "uyarlamali_karar": adaptive_reason,
                        "sahne_politikasi": scene_reason,
                        "uygulanan_karar": applied_decision,
                        "ilk_takas_eklenen": [_detail(item) for item in first_added],
                        "ilk_takas_cikarilan": [_detail(item) for item in first_removed],
                        "ikinci_takas_eklenen": [_detail(item) for item in second_added],
                        "ikinci_takas_cikarilan": [_detail(item) for item in second_removed],
                        "eklenen": [_detail(item) for item in final_added],
                        "cikarilan": [_detail(item) for item in final_removed],
                        "en_guclu_yan_kumeler": [
                            _detail(item) for item in ranked_sidecars[:5]
                        ],
                    }
                )
            except Exception as exc:
                record["durum"] = "denetim_hatasi"
                record["hata"] = f"{type(exc).__name__}: {exc}"
                errors.append(f"{region_key}: {type(exc).__name__}: {exc}")
            records[region_key] = record

    payload = {
        "rapor_tarihi": report_date,
        "olusturma": now.strftime("%Y-%m-%d %H:%M %Z"),
        "amac": (
            "Diyagonal 800-10.000 m² yan-kümeleri yalnız yerini aldıkları normal "
            "şantiye-ölçeği adayından daha güçlü Sentinel kanıtı taşıyorsa, toplam "
            "24 aday ve ölçek dağılımını değiştirmeden terfi ettirmek; ilk yan-küme "
            "için de güvenli karşılaştırma uygulamak ve mevcut sahneyi geriye dönük "
            "değiştirmemek."
        ),
        "bolgeler": records,
    }
    AUDIT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if applied:
        build_daily_report()
    return payload, applied, skipped, errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print(
            "Uyarlamalı diyagonal yan-küme öz testi başarılı: ilk ve ikinci yan-küme "
            "yalnız yerini aldığı normal adaydan daha güçlü ise bire bir terfi ediyor; "
            "zayıf yan-küme reddediliyor, 24 aday ve ölçek dağılımı sabit kalıyor."
        )
        return

    payload, applied, skipped, errors = run_guard()
    summaries = []
    for key, item in payload["bolgeler"].items():
        summaries.append(
            f"{key}: ilk={item.get('ilk_yan_kume_karari', item.get('durum'))}; "
            f"ikinci={item.get('ikinci_yan_kume_karari', 'yok')}; "
            f"uygulama={item.get('uygulanan_karar', 'yok')}"
        )
    print("Uyarlamalı diyagonal yan-küme koruması: " + " | ".join(summaries))
    if skipped:
        print("Korunan/atlanan bölgeler: " + ", ".join(sorted(set(skipped))))
    if errors:
        raise RuntimeError("Uyarlamalı yan-küme hatası: " + " | ".join(errors))


if __name__ == "__main__":
    main()
