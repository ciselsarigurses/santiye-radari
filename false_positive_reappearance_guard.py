"""Sahada yanlış pozitif denmiş noktadaki gerçek yeni temel/kazı sinyalini kaçırma.

Bir Sentinel görevi ``YANLIS_POZITIF`` olarak kapatıldığında mevcut görev eşleştirme
mantığı aynı konumdaki sonraki hotspot'u 25 m içinde aynı ``KONTROL_EDILDI`` göreve
bağlayabilir. Bu doğru bir tekrar-gürültü korumasıdır; ancak haftalar/günler sonra
aynı parselde gerçekten hafriyat başlarsa yeni sinyal sessizce kapalı kalmamalıdır.

Saha kalibrasyonu, tek başına yeni yüzey/toprak değişiminin tarla düzeltmesini tekrar
sahaya taşıyabildiğini gösterdi. Bu nedenle yeniden açma artık yalnız şu dar durumda
izinlidir:
- sonuç ``YANLIS_POZITIF`` ve görev hâlâ ``KONTROL_EDILDI``;
- gerçekten daha yeni bir Sentinel sahnesi gelmiş ve ``yeni_goruntu`` true;
- yeniden-beliren sinyal 250-2.000 m² erken/parsel ana alarm bandında;
- 250-800 m² ise uydu motorunun güçlü küçük-saha sınıfından geçmiş;
- 800-2.000 m² ise zaten ana üretim seçimine girmiş parsel-ölçekli hotspot;
- yeni hotspot, eski saha sonucunun koordinatına en fazla 25 m uzakta;
- aynı Sentinel tarihi için üretilmiş lokal-seed merkezli temel/kazı morfolojisi
  hotspot'u en fazla 45 m uzakta ve YÜKSEK (>=65) olasılık düzeyindedir.

Morfoloji çıktısı eksik veya eskiyse koruma güvenli tarafta kalır ve görevi yeniden
açmaz. Temel-kazısı diagnostik workflow'u güncel çıktıyı kaydettiğinde guard yeniden
çalıştırılır. Böylece sahada tarla/bahçe olduğu doğrulanmış bir yer yalnız spektral
renk/nem/kuruma değişimiyle tekrar saha rotasına dönmez.

Bu üst sınır ``field_state.EARLY_SITE_MATCH_MAX_M2`` ile aynıdır. 150-249 m² MİKRO
ŞANTİYE bu koruma ile üretim görevine yükseltilmez; 2.000 m² üzerindeki geniş yüzey
hareketleri de otomatik yeniden açılmaz. ``TARLA_BITKI`` için mevcut ayrı takip
mekanizmasına dokunulmaz. Sentinel-2 10 m veriden gerçek kazı derinliği ölçüldüğü
iddia edilmez; kullanılan morfoloji yalnız temel/derin kazı olasılığı proxy'sidir.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from daily_report import ISTANBUL, _report_hotspots, ensure_daily_schema
from field_outcome import ensure_outcome_schema
from field_state import EARLY_SITE_MATCH_MAX_M2, ensure_state_schema
from scanner import connect


MIN_AREA_M2 = 250
STRONG_SMALL_MAX_M2 = 800
EARLY_SITE_MAX_M2 = EARLY_SITE_MATCH_MAX_M2
MAX_MATCH_METERS = 25
MORPHOLOGY_MATCH_METERS = 45
MORPHOLOGY_MIN_SCORE = 65
MORPHOLOGY_REVIEW = Path(__file__).with_name("seed_centered_excavation_morphology_review.json")


def _now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _scene_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    return None


def _distance_m(lat1, lon1, lat2, lon2):
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _area_m2(item):
    try:
        return float(item.get("alan_m2") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _strong_small_site(item):
    area_m2 = _area_m2(item)
    if not (MIN_AREA_M2 <= area_m2 <= STRONG_SMALL_MAX_M2):
        return False
    size_class = str(item.get("boyut_sinifi") or "").strip().upper()
    signal = str(item.get("sinyal") or "").casefold()
    return size_class == "KUCUK" or "küçük, güçlü" in signal


def _eligible_early_site(item):
    """Üretimde zaten seçilmiş erken/parsel adayı güvenli yeniden-açma sınıfına al."""
    area_m2 = _area_m2(item)
    if not (MIN_AREA_M2 <= area_m2 <= EARLY_SITE_MAX_M2):
        return False
    if area_m2 <= STRONG_SMALL_MAX_M2:
        return _strong_small_site(item)
    # 800-2.000 m² için ek alarm üretmiyoruz: _report_hotspots yalnız ana üretim
    # hareket_json listesine seçilmiş hotspot'ları döndürür. Bu sınıf field_state
    # tarafından da 25 m erken/parsel eşleşme korumasıyla ele alınır.
    return True


def _load_morphology_review(path=MORPHOLOGY_REVIEW):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _has_current_excavation_morphology(item, review):
    """Aynı sahne tarihindeki YÜKSEK lokal-kazı morfolojisi desteğini doğrula."""
    if not isinstance(review, dict):
        return False
    current_date = _scene_date(item.get("son_tarih"))
    try:
        latitude = float(item.get("enlem"))
        longitude = float(item.get("boylam"))
    except (TypeError, ValueError, AttributeError):
        return False
    if current_date is None:
        return False

    for region in (review.get("bolgeler") or {}).values():
        if not isinstance(region, dict):
            continue
        if _scene_date(region.get("son_tarih")) != current_date:
            continue
        for candidate in region.get("adaylar") or []:
            if not isinstance(candidate, dict):
                continue
            try:
                score = float(candidate.get("seed_merkezli_morfoloji_puani") or 0)
                level = str(candidate.get("seed_merkezli_morfoloji_seviyesi") or "").upper()
                distance = _distance_m(
                    latitude,
                    longitude,
                    float(candidate.get("enlem")),
                    float(candidate.get("boylam")),
                )
            except (TypeError, ValueError, AttributeError):
                continue
            if (
                score >= MORPHOLOGY_MIN_SCORE
                and level == "YUKSEK"
                and distance <= MORPHOLOGY_MATCH_METERS
            ):
                return True
    return False


def _safe_new_reappearance(saved_scene, item, morphology_review):
    if not bool(item.get("yeni_goruntu")) or not _eligible_early_site(item):
        return False
    saved = _scene_date(saved_scene)
    current = _scene_date(item.get("son_tarih"))
    if not (saved and current and current > saved):
        return False
    return _has_current_excavation_morphology(item, morphology_review)


def _report_date(connection):
    row = connection.execute(
        "SELECT MAX(rapor_tarihi) FROM gunluk_uydu_raporlari"
    ).fetchone()
    value = str((row or [None])[0] or "").strip()
    return value or datetime.now(ISTANBUL).strftime("%Y-%m-%d")


def reopen_false_positive_reappearances():
    """Yeni + lokal temel/kazı morfolojisiyle dönen tamamlanmış yanlış pozitifleri aç."""
    ensure_daily_schema()
    reopened = []
    morphology_review = _load_morphology_review()
    with connect() as connection:
        ensure_state_schema(connection)
        ensure_outcome_schema(connection)
        report_date = _report_date(connection)
        current = _report_hotspots(connection, report_date)
        rows = connection.execute(
            """SELECT s.gorev_id,s.enlem,s.boylam,s.son_tarih
            FROM saha_sonuclari s
            JOIN saha_durumlari d ON d.gorev_id=s.gorev_id
            WHERE s.sonuc='YANLIS_POZITIF'
            AND d.kaynak='uydu' AND d.durum='KONTROL_EDILDI'
            AND s.enlem IS NOT NULL AND s.boylam IS NOT NULL"""
        ).fetchall()

        for task_id, old_lat, old_lon, saved_scene in rows:
            try:
                old_lat = float(old_lat)
                old_lon = float(old_lon)
            except (TypeError, ValueError):
                continue
            matches = []
            for item in current:
                if not _safe_new_reappearance(saved_scene, item, morphology_review):
                    continue
                try:
                    distance = _distance_m(
                        old_lat,
                        old_lon,
                        float(item.get("enlem")),
                        float(item.get("boylam")),
                    )
                except (TypeError, ValueError):
                    continue
                if distance <= MAX_MATCH_METERS:
                    matches.append((distance, item))
            if not matches:
                continue

            distance, item = min(matches, key=lambda value: value[0])
            cursor = connection.execute(
                """UPDATE saha_durumlari
                SET durum='KONTROLE_GIT',son_islem=?
                WHERE gorev_id=? AND durum='KONTROL_EDILDI'""",
                (_now_utc(), str(task_id)),
            )
            if cursor.rowcount:
                reopened.append(
                    {
                        "gorev_id": str(task_id),
                        "mesafe_m": round(distance, 1),
                        "alan_m2": int(round(float(item.get("alan_m2") or 0))),
                        "son_tarih": item.get("son_tarih"),
                    }
                )
    return reopened


def _self_check():
    base = {
        "enlem": 38.30,
        "boylam": 26.30,
        "alan_m2": 400,
        "boyut_sinifi": "KUCUK",
        "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
        "son_tarih": "03.09.2026",
        "yeni_goruntu": True,
    }
    morphology = {
        "bolgeler": {
            "test": {
                "son_tarih": "03.09.2026",
                "adaylar": [
                    {
                        "enlem": 38.30,
                        "boylam": 26.30,
                        "seed_merkezli_morfoloji_puani": 70,
                        "seed_merkezli_morfoloji_seviyesi": "YUKSEK",
                    }
                ],
            }
        }
    }
    assert EARLY_SITE_MAX_M2 == 2_000
    assert _safe_new_reappearance("29.08.2026", base, morphology)
    assert not _safe_new_reappearance("29.08.2026", base, {})
    assert not _safe_new_reappearance(
        "29.08.2026",
        base,
        {"bolgeler": {"test": {"son_tarih": "02.09.2026", "adaylar": morphology["bolgeler"]["test"]["adaylar"]}}},
    )
    distant = json.loads(json.dumps(morphology))
    distant["bolgeler"]["test"]["adaylar"][0]["enlem"] = 38.31
    assert not _safe_new_reappearance("29.08.2026", base, distant)
    weak = json.loads(json.dumps(morphology))
    weak["bolgeler"]["test"]["adaylar"][0]["seed_merkezli_morfoloji_puani"] = 64
    assert not _safe_new_reappearance("29.08.2026", base, weak)
    assert not _safe_new_reappearance("03.09.2026", base, morphology)
    assert not _safe_new_reappearance(
        "29.08.2026", dict(base, yeni_goruntu=False), morphology
    )
    assert not _safe_new_reappearance(
        "29.08.2026", dict(base, alan_m2=200), morphology
    )
    assert not _safe_new_reappearance(
        "29.08.2026",
        dict(base, alan_m2=700, boyut_sinifi="STANDART", sinyal="Yüzey değişimi adayı"),
        morphology,
    )
    parsel = dict(
        base,
        alan_m2=1_100,
        boyut_sinifi="STANDART",
        sinyal="Yüzey değişimi adayı",
    )
    assert _safe_new_reappearance("29.08.2026", parsel, morphology)
    assert _safe_new_reappearance("29.08.2026", dict(parsel, alan_m2=2_000), morphology)
    assert not _safe_new_reappearance("29.08.2026", dict(parsel, alan_m2=2_001), morphology)
    assert _distance_m(38.30, 26.30, 38.30, 26.30) == 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    _self_check()
    if args.check_only:
        print(
            "Yanlış pozitif yeniden-belirme koruması öz testi başarılı: "
            "daha yeni 250-2.000 m² erken/parsel kanıtına ek olarak aynı sahne "
            "tarihli YÜKSEK lokal-seed temel/kazı morfolojisi zorunlu."
        )
        return

    reopened = reopen_false_positive_reappearances()
    if reopened:
        for item in reopened:
            print(
                "Yeniden açıldı: {gorev_id} · {alan_m2} m² · {mesafe_m} m · {son_tarih}".format(
                    **item
                )
            )
    else:
        print(
            "Daha yeni ve YÜKSEK lokal temel/kazı morfolojisiyle doğrulanan kapalı yanlış pozitif yok."
        )


if __name__ == "__main__":
    main()
