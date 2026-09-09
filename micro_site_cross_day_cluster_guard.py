"""MİKRO kısa listede farklı Sentinel günlerini aynı geniş-yüzey kümesi saymayı önler.

Çeşme ve Uzunkuyu/Gülbahçe kutuları yeni Sentinel sahnesine farklı günlerde
geçebilir. Aynı 250 m yarıçapındaki MİKRO izler farklı kaynak günlerine aitse bunlar
tek bir eşzamanlı geniş tarla/toprak hareketinin komşuları değildir. Eski davranış
bu izleri birlikte sayabildiği için yeni veya devam eden kompakt bir MİKRO sinyali
arka plana indirebilirdi.

Bu dosya yalnız diagnostik kısa-listenin komşuluk hesabını kaynak Sentinel günüyle
sınırlar. Ana 250 m² üretim eşiğine, alarm veya saha görevi üretimine dokunmaz.
"""

from __future__ import annotations

import micro_site_shortlist as base


def _annotate_same_source_day(rows, production):
    annotated = []
    for index, row in enumerate(rows):
        others = [
            other
            for other_index, other in enumerate(rows)
            if other_index != index and base._same_source_day(row, other)
        ]
        neighbor_count = sum(
            base._distance_m(row, other) <= base.CLUSTER_RADIUS_M for other in others
        )
        row_production = base._production_for_micro_row(row, production)
        production_distance = base._nearest_distance(row, row_production)

        updated = dict(row)
        updated["250m_mikro_komsu"] = int(neighbor_count)
        updated["genis_hareket_kumesi_riski"] = bool(
            neighbor_count > base.MAX_CLUSTER_NEIGHBORS
        )
        updated["250plus_yakinlik_kaynak_tarihi"] = (
            str(updated.get("mikro_kaynak_sentinel_tarihi") or "").strip() or None
        )
        updated["en_yakin_250plus_m"] = (
            round(production_distance) if production_distance is not None else None
        )
        updated["ana_adaya_yakin"] = bool(
            production_distance is not None
            and production_distance <= base.PRODUCTION_NEAR_RADIUS_M
        )
        updated["spektral_kisa_liste_kapisi"] = bool(
            base._number(updated.get("ortalama_rgb_degisim"), 0.0)
            >= base.MIN_RGB_CHANGE
            and base._number(updated.get("ortalama_ndvi_kaybi"), 0.0)
            >= base.MIN_NDVI_LOSS
        )
        annotated.append(updated)
    return annotated


def _self_check():
    def row(lat, lon, date):
        return {
            "enlem": lat,
            "boylam": lon,
            "alan_m2": 200,
            "mikro_kaynak_sentinel_tarihi": date,
            "ortalama_rgb_degisim": 0.40,
            "ortalama_ndvi_kaybi": 0.30,
            "ortalama_parlaklik_artisi": 0.20,
        }

    current = row(38.33000, 26.64500, "08.09.2026")
    older_neighbors = [
        row(38.33020, 26.64500, "05.09.2026"),
        row(38.33000, 26.64520, "05.09.2026"),
        row(38.32980, 26.64500, "05.09.2026"),
    ]
    checked = _annotate_same_source_day([current, *older_neighbors], [])[0]
    assert checked["250m_mikro_komsu"] == 0, (
        "Farklı Sentinel günündeki yakın MİKRO izler yeni adayın geniş-yüzey "
        "komşusu sayılamaz."
    )
    assert checked["genis_hareket_kumesi_riski"] is False

    same_day_neighbors = [
        row(38.33020, 26.64500, "08.09.2026"),
        row(38.33000, 26.64520, "08.09.2026"),
        row(38.32980, 26.64500, "08.09.2026"),
    ]
    checked = _annotate_same_source_day([current, *same_day_neighbors], [])[0]
    assert checked["250m_mikro_komsu"] == 3
    assert checked["genis_hareket_kumesi_riski"] is True, (
        "Aynı Sentinel günündeki yoğun komşuluk geniş-yüzey korumasında kalmalıdır."
    )


def main():
    _self_check()
    # Mevcut kısa-liste motorunun yalnız komşuluk anotasyonunu daralt. Böylece
    # dedupe, 250+ yakınlık, spektral kapı, limit ve çıktı şeması tek yerde kalır.
    base._annotate = _annotate_same_source_day
    base.main()


if __name__ == "__main__":
    main()
