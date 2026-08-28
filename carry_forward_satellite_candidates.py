"""Yeni Sentinel görüntüsü yokken açık uydu adaylarının ayrıntılarını korur.

``daily_report.py`` yeni görüntü gelmeyen günlerde bugünün uydu satırını bilinçli
olarak boş hareket listesiyle oluşturur. Saha görevleri ayrı durum tablosunda
kalıcıdır; ancak alan, boyut sınıfı ve son spektral sinyal gibi ayrıntılar o günkü
raporda 0 m² / genel "bekleyen" bilgisine düşebiliyordu. Bu yardımcı yalnızca
analiz yapılmadığını açıkça gösteren boş günlük satırlarda, aynı Sentinel öğesine
ait son gerçek analiz sonucunu taşır. Yeni bir analiz gerçekten çalıştıysa
(değişim metrikleri doluysa) eski sonucu geri getirmez.
"""

from __future__ import annotations

import json
from datetime import datetime

from daily_report import ISTANBUL, ensure_daily_schema
from scanner import connect


def _movement_list(raw):
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def carry_forward_candidates(report_date=None):
    ensure_daily_schema()
    report_date = report_date or datetime.now(ISTANBUL).strftime("%Y-%m-%d")
    copied = []

    with connect() as connection:
        rows = connection.execute(
            """SELECT bolge,son_item,hareket_json,yeni_goruntu,hata,
            degisim_km2,degisim_yuzde
            FROM gunluk_uydu_raporlari
            WHERE rapor_tarihi=?""",
            (report_date,),
        ).fetchall()

        for region_key, latest_item, movement_json, has_new, error, km2, percent in rows:
            # Gerçek yeni görüntü, hata veya aynı gün yeniden çalışmış gerçek analiz
            # varsa bugünkü sonuca dokunma. daily_report'ın "yeni görüntü yok"
            # yer tutucusunda ise iki değişim metriği de NULL kalır.
            if has_new or error or km2 is not None or percent is not None:
                continue
            if not latest_item or _movement_list(movement_json):
                continue

            previous = connection.execute(
                """SELECT hareket_json,degisim_km2,degisim_yuzde,bulut_yuzde
                FROM gunluk_uydu_raporlari
                WHERE bolge=? AND rapor_tarihi<? AND son_item=?
                  AND hata IS NULL
                  AND (degisim_km2 IS NOT NULL OR degisim_yuzde IS NOT NULL)
                ORDER BY rapor_tarihi DESC LIMIT 1""",
                (region_key, report_date, latest_item),
            ).fetchone()
            if not previous:
                continue

            movement = _movement_list(previous[0])
            if not movement:
                # Son gerçek analiz de aday üretmediyse boşluk doğrudur.
                continue

            connection.execute(
                """UPDATE gunluk_uydu_raporlari SET
                hareket_json=?,degisim_km2=?,degisim_yuzde=?,bulut_yuzde=?
                WHERE rapor_tarihi=? AND bolge=?
                  AND yeni_goruntu=0 AND hata IS NULL
                  AND degisim_km2 IS NULL AND degisim_yuzde IS NULL""",
                (
                    previous[0],
                    previous[1],
                    previous[2],
                    previous[3],
                    report_date,
                    region_key,
                ),
            )
            copied.append((str(region_key), len(movement)))

    return copied


if __name__ == "__main__":
    copied = carry_forward_candidates()
    if copied:
        detail = ", ".join(f"{region}={count} aday" for region, count in copied)
        print("Yeni görüntü yok; son gerçek uydu aday ayrıntıları korundu: " + detail)
    else:
        print("Taşınması gereken bekleyen uydu aday ayrıntısı yok.")
