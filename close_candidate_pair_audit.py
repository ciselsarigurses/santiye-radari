"""Aynı Sentinel çiftindeki birbirine çok yakın taze saha adaylarını diagnostik olarak izler.

Amaç, yaklaşık 10 m Sentinel ızgarasında tek bir hafriyatın iki ayrı bağlı bileşene
bölünmesi ile gerçekten yan yana iki ayrı küçük şantiyeyi birbirinden ayırmak için
saha kalibrasyonuna görünür kanıt sağlamaktır. Bu katman hiçbir adayı birleştirmez,
silmez, yeniden sıralamaz; alarm veya saha görevi üretmez.

Yeni Sentinel sahnesinin ilk işlendiği turda ``yeni_goruntu=True`` olur; aynı sahne
saatlik rapor yenilemesinde yeni sayılmadığında ise yakın-çift kanıtı bir anda
kaybolmamalıdır. Bu nedenle yalnız hâlâ eyleme dönük olan, uydu kanıt yaşı en fazla
iki gün olan aynı güçlü küçük-saha adayları diagnostik olarak kısa süre korunur.
150–249 m² MİKRO ŞANTİYE bu katmana girmez ve üretim alarmına yükseltilmez.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


REPORT_FILE = Path(__file__).with_name("latest_report.json")
OUTPUT_FILE = Path(__file__).with_name("close_candidate_pair_audit.json")
ISTANBUL = ZoneInfo("Europe/Istanbul")
PAIR_MAX_DISTANCE_M = 60
PAIR_MIN_DISTANCE_M = 10
MIN_AREA_M2 = 250
MAX_SMALL_AREA_M2 = 800
DIAGNOSTIC_RETENTION_DAYS = 2
ACTIONABLE_STATUSES = {"KONTROLE_GIT", "TEKRAR_GIT"}


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _point(item):
    latitude = _number(item.get("enlem"))
    longitude = _number(item.get("boylam"))
    if latitude is None or longitude is None:
        return None
    return latitude, longitude


def _distance_m(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    mean_lat = math.radians((lat1 + lat2) / 2)
    north = (lat1 - lat2) * 110570
    east = (lon1 - lon2) * 111320 * math.cos(mean_lat)
    return math.hypot(north, east)


def _strong_small(item):
    area = _number(item.get("alan_m2"))
    if area is None or not (MIN_AREA_M2 <= area <= MAX_SMALL_AREA_M2):
        return False
    size_class = str(item.get("boyut_sinifi") or "").strip().upper()
    signal = str(item.get("sinyal") or "").casefold()
    return size_class == "KUCUK" or "küçük, güçlü" in signal


def _fresh(item):
    """İlk yeni-sahne turunu ve yalnız kısa süre korunmuş güncel kanıtı kabul et."""
    if str(item.get("son_tarih") or "").strip() == "":
        return False
    if bool(item.get("yeni_goruntu")):
        return True

    status = str(item.get("saha_durumu") or "").strip().upper()
    if status not in ACTIONABLE_STATUSES:
        return False

    evidence_age = _number(item.get("uydu_kanit_yasi_gun"))
    wait_age = _number(item.get("bekleme_gun"))
    if evidence_age is None or not (0 <= evidence_age <= DIAGNOSTIC_RETENTION_DAYS):
        return False
    if wait_age is None or not (0 <= wait_age <= DIAGNOSTIC_RETENTION_DAYS):
        return False

    # Tarihsel eşleşmeyle taşınmış eski görevleri taze geometri kanıtı sayma.
    historical_distance = _number(item.get("tarihsel_esleme_mesafe_m"))
    if historical_distance is not None:
        return False
    note = str(item.get("konum_notu") or "").casefold()
    if "tarihsel eşleş" in note or "tarihsel esles" in note:
        return False

    return True


def find_close_pairs(candidates):
    eligible = [
        item
        for item in candidates or []
        if isinstance(item, dict) and _fresh(item) and _strong_small(item) and _point(item)
    ]
    pairs = []
    for index, first in enumerate(eligible):
        for second in eligible[index + 1:]:
            if str(first.get("bolge") or "") != str(second.get("bolge") or ""):
                continue
            if str(first.get("onceki_tarih") or "") != str(second.get("onceki_tarih") or ""):
                continue
            if str(first.get("son_tarih") or "") != str(second.get("son_tarih") or ""):
                continue
            first_point = _point(first)
            second_point = _point(second)
            distance = _distance_m(first_point, second_point)
            if not (PAIR_MIN_DISTANCE_M <= distance <= PAIR_MAX_DISTANCE_M):
                continue
            midpoint = (
                (first_point[0] + second_point[0]) / 2,
                (first_point[1] + second_point[1]) / 2,
            )
            pairs.append(
                {
                    "durum": "SAHADA_AYRI_MI_TEK_KUME_MI_DOGRULA",
                    "alarm": False,
                    "saha_gorevi": False,
                    "bolge": str(first.get("bolge") or ""),
                    "onceki_tarih": str(first.get("onceki_tarih") or ""),
                    "son_tarih": str(first.get("son_tarih") or ""),
                    "mesafe_m": round(distance, 1),
                    "ortak_kontrol_merkezi": {
                        "enlem": round(midpoint[0], 6),
                        "boylam": round(midpoint[1], 6),
                    },
                    "adaylar": [
                        {
                            "gorev_id": str(first.get("gorev_id") or ""),
                            "enlem": round(first_point[0], 6),
                            "boylam": round(first_point[1], 6),
                            "alan_m2": round(_number(first.get("alan_m2")) or 0),
                            "mahalle": str(first.get("mahalle") or ""),
                        },
                        {
                            "gorev_id": str(second.get("gorev_id") or ""),
                            "enlem": round(second_point[0], 6),
                            "boylam": round(second_point[1], 6),
                            "alan_m2": round(_number(second.get("alan_m2")) or 0),
                            "mahalle": str(second.get("mahalle") or ""),
                        },
                    ],
                    "yorum": (
                        "Aynı Sentinel görüntü çiftinde iki güçlü küçük aday 60 m içinde. "
                        "Otomatik birleştirilmez: tek hafriyatın maskede bölünmesi veya yan yana "
                        "iki ayrı saha olabilir. Saha geri bildirimi bu ayrımı kalibre etmelidir."
                    ),
                }
            )
    pairs.sort(key=lambda item: (item["mesafe_m"], item["bolge"], item["son_tarih"]))
    return pairs


def _semantic(payload):
    if not isinstance(payload, dict):
        return payload
    return {key: value for key, value in payload.items() if key != "olusturma"}


def _write_if_changed(payload):
    old = None
    if OUTPUT_FILE.exists():
        try:
            old = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            old = None
    if _semantic(old) == _semantic(payload):
        return False
    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def _self_test():
    base = {
        "yeni_goruntu": True,
        "boyut_sinifi": "KUCUK",
        "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
        "alan_m2": 400,
        "bolge": "test",
        "onceki_tarih": "29.08.2026",
        "son_tarih": "03.09.2026",
        "saha_durumu": "KONTROLE_GIT",
        "uydu_kanit_yasi_gun": 0,
        "bekleme_gun": 0,
    }
    near_a = {**base, "gorev_id": "A", "enlem": 38.278453, "boylam": 26.309577}
    near_b = {**base, "gorev_id": "B", "enlem": 38.278544, "boylam": 26.309921}
    assert len(find_close_pairs([near_a, near_b])) == 1, "Yakın taze küçük aday çifti bulunamadı."

    # Aynı Sentinel sahnesi saatlik yenilemede yeni sayılmasa da 2 günlük güncel kanıt
    # penceresinde yakın çift diagnostik görünürlüğünü korumalıdır.
    retained_a = {
        **near_a,
        "yeni_goruntu": False,
        "uydu_kanit_yasi_gun": 1,
        "bekleme_gun": 1,
    }
    retained_b = {
        **near_b,
        "yeni_goruntu": False,
        "uydu_kanit_yasi_gun": 1,
        "bekleme_gun": 1,
    }
    assert len(find_close_pairs([retained_a, retained_b])) == 1, (
        "Saatlik yenilemede yakın-çift diagnostik kanıtı erken kayboldu."
    )

    stale_b = {**retained_b, "uydu_kanit_yasi_gun": DIAGNOSTIC_RETENTION_DAYS + 1}
    assert not find_close_pairs([retained_a, stale_b]), "Eski uydu kanıtı yanlışlıkla taze sayıldı."

    historical_b = {**retained_b, "tarihsel_esleme_mesafe_m": 12.0}
    assert not find_close_pairs([retained_a, historical_b]), (
        "Tarihsel taşınmış görev yanlışlıkla taze yakın çift sayıldı."
    )

    closed_b = {**retained_b, "saha_durumu": "KONTROL_EDILDI"}
    assert not find_close_pairs([retained_a, closed_b]), "Kapalı görev diagnostik çifte taşındı."

    far = {**near_b, "gorev_id": "C", "enlem": 38.280000, "boylam": 26.311000}
    assert not find_close_pairs([near_a, far]), "Uzak adaylar yanlışlıkla çift sayıldı."

    old_scene = {**near_b, "gorev_id": "D", "son_tarih": "26.08.2026"}
    assert not find_close_pairs([near_a, old_scene]), "Farklı Sentinel çiftleri yanlış eşleştirildi."

    micro = {**near_b, "gorev_id": "E", "alan_m2": 200}
    assert not find_close_pairs([near_a, micro]), "150-249 m² mikro katman üretim çifti sanıldı."


def main():
    _self_test()
    if not REPORT_FILE.exists():
        raise SystemExit("latest_report.json bulunamadı; yakın aday diagnostigi çalıştırılamadı.")
    payload = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    pairs = find_close_pairs(payload.get("saha_adaylari") or [])
    output = {
        "olusturma": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M %z"),
        "alarm": False,
        "saha_gorevi": False,
        "ana_uretim_esigi_m2": MIN_AREA_M2,
        "yakın_cift_ust_sinir_m": PAIR_MAX_DISTANCE_M,
        "diagnostik_kanit_koruma_gun": DIAGNOSTIC_RETENTION_DAYS,
        "amac": (
            "Aynı Sentinel çiftindeki yakın 250-800 m² güçlü küçük adayların tek bölünmüş "
            "hafriyat mı, yoksa yan yana ayrı şantiyeler mi olduğunu saha kalibrasyonuyla ayırmak."
        ),
        "otomatik_birlestirme": False,
        "cift_sayisi": len(pairs),
        "ciftler": pairs,
    }
    changed = _write_if_changed(output)
    print(f"Yakın aday geometri denetimi: {len(pairs)} çift; dosya_değişti={str(changed).lower()}.")
    for pair in pairs[:8]:
        first, second = pair["adaylar"]
        print(
            f"- {first['gorev_id']} ↔ {second['gorev_id']} · {pair['mesafe_m']} m · "
            f"{first['alan_m2']}+{second['alan_m2']} m² · otomatik birleştirme yok"
        )


if __name__ == "__main__":
    main()
