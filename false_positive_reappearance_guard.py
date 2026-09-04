"""Sahada yanlış pozitif denmiş noktadaki yeni küçük-hafriyat sinyalini kaçırma.

Bir Sentinel görevi ``YANLIS_POZITIF`` olarak kapatıldığında mevcut görev eşleştirme
mantığı aynı konumdaki sonraki hotspot'u 25 m içinde aynı ``KONTROL_EDILDI`` göreve
bağlayabilir. Bu doğru bir tekrar-gürültü korumasıdır; ancak haftalar/günler sonra
aynı parselde gerçekten hafriyat başlarsa yeni sinyal sessizce kapalı kalmamalıdır.

Bu koruma yalnız şu dar durumda görevi yeniden açar:
- sonuç ``YANLIS_POZITIF`` ve görev hâlâ ``KONTROL_EDILDI``;
- gerçekten daha yeni bir Sentinel sahnesi gelmiş ve ``yeni_goruntu`` true;
- yeniden-beliren sinyal 250-800 m² ana alarm bandında;
- uydu motorunun güçlü küçük-saha sınıfından geçmiş;
- yeni hotspot, eski saha sonucunun koordinatına en fazla 25 m uzakta.

150-249 m² MİKRO ŞANTİYE bu koruma ile üretim görevine yükseltilmez. Geniş yüzey
hareketleri de otomatik yeniden açılmaz. ``TARLA_BITKI`` için mevcut ayrı takip
mekanizmasına dokunulmaz.
"""

from __future__ import annotations

import argparse
import math
from datetime import datetime, timezone

from daily_report import ISTANBUL, _report_hotspots, ensure_daily_schema
from field_outcome import ensure_outcome_schema
from field_state import ensure_state_schema
from scanner import connect


MIN_AREA_M2 = 250
MAX_AREA_M2 = 800
MAX_MATCH_METERS = 25


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


def _strong_small_site(item):
    try:
        area_m2 = float(item.get("alan_m2") or 0)
    except (TypeError, ValueError, AttributeError):
        return False
    if not (MIN_AREA_M2 <= area_m2 <= MAX_AREA_M2):
        return False
    size_class = str(item.get("boyut_sinifi") or "").strip().upper()
    signal = str(item.get("sinyal") or "").casefold()
    return size_class == "KUCUK" or "küçük, güçlü" in signal


def _safe_new_reappearance(saved_scene, item):
    if not bool(item.get("yeni_goruntu")) or not _strong_small_site(item):
        return False
    saved = _scene_date(saved_scene)
    current = _scene_date(item.get("son_tarih"))
    return bool(saved and current and current > saved)


def _report_date(connection):
    row = connection.execute(
        "SELECT MAX(rapor_tarihi) FROM gunluk_uydu_raporlari"
    ).fetchone()
    value = str((row or [None])[0] or "").strip()
    return value or datetime.now(ISTANBUL).strftime("%Y-%m-%d")


def reopen_false_positive_reappearances():
    """Yeni güçlü küçük-saha kanıtıyla dönen tamamlanmış yanlış pozitifleri aç."""
    ensure_daily_schema()
    reopened = []
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
                if not _safe_new_reappearance(saved_scene, item):
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
        "alan_m2": 400,
        "boyut_sinifi": "KUCUK",
        "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
        "son_tarih": "03.09.2026",
        "yeni_goruntu": True,
    }
    assert _safe_new_reappearance("29.08.2026", base)
    assert not _safe_new_reappearance("03.09.2026", base)
    assert not _safe_new_reappearance("29.08.2026", dict(base, yeni_goruntu=False))
    assert not _safe_new_reappearance("29.08.2026", dict(base, alan_m2=200))
    assert not _safe_new_reappearance("29.08.2026", dict(base, alan_m2=900))
    assert not _safe_new_reappearance(
        "29.08.2026",
        dict(base, boyut_sinifi="STANDART", sinyal="Yüzey değişimi adayı"),
    )
    assert _distance_m(38.30, 26.30, 38.30, 26.30) == 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    _self_check()
    if args.check_only:
        print(
            "Yanlış pozitif yeniden-belirme koruması öz testi başarılı: "
            "yalnız daha yeni 250-800 m² güçlü küçük-saha kanıtı kabul ediliyor."
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
        print("Daha yeni güçlü küçük-saha kanıtıyla dönen kapalı yanlış pozitif yok.")


if __name__ == "__main__":
    main()
