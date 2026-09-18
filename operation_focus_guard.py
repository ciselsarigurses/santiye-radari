"""Günlük saha rotasını Çeşme–Uzunkuyu operasyon odağında tutar.

Kullanıcı yönlendirmesine göre Gülbahçe şu an günlük ekip rotası önceliği değildir.
Ayrıca ``manual_field_feedback.json`` içinde ``MEVCUT_MUSTERI`` olarak doğrulanan
konumlar gerçek şantiye sinyali sayılmaya devam eder, fakat yeni müşteri saha
rotasına tekrar gönderilmez. Bu ayrım model kalibrasyonunu bozmadan operasyonel
zaman kaybını önler.

Bu guard aday kayıtlarını silmez. Gülbahçe adaylarını ``rota_uygun=False`` ile
arka plana alır; Gülbahçe kör-alan devriyesini de operasyonel saha rotasından
çıkarıp diagnostik arka planda korur. Son aşamada kullanıcıya gösterilen
``SAHA_RAPORU.md`` içinde de açık Gülbahçe saha yönlendirmelerini temizler; böylece
JSON doğru olsa bile daha eski bir markdown üretim adımı Gülbahçe'yi yeniden saha
rotası gibi gösteremez. Uzunkuyu, Germiyan, Ildır veya mevkii doğrulanmamış adayları
coğrafi tahminle elemez.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
REPORT_FILE = BASE_DIR / "latest_report.json"
FIELD_REPORT_FILE = BASE_DIR / "SAHA_RAPORU.md"
FEEDBACK_FILE = BASE_DIR / "manual_field_feedback.json"
LOW_PRIORITY_NEIGHBORHOODS = {"gülbahçe"}
KNOWN_CUSTOMER_OUTCOME = "MEVCUT_MUSTERI"
DEFAULT_CUSTOMER_RADIUS_M = 30.0
MAX_CUSTOMER_RADIUS_M = 50.0


def _normalize(value: Any) -> str:
    return str(value or "").strip().casefold()


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _distance_m(first: dict[str, Any], second: dict[str, Any]) -> float:
    lat1 = _number(first.get("enlem"))
    lon1 = _number(first.get("boylam"))
    lat2 = _number(second.get("enlem"))
    lon2 = _number(second.get("boylam"))
    if None in (lat1, lon1, lat2, lon2):
        return float("inf")
    mean_lat = math.radians((float(lat1) + float(lat2)) / 2.0)
    north = (float(lat2) - float(lat1)) * 110570.0
    east = (float(lon2) - float(lon1)) * 111320.0 * math.cos(mean_lat)
    return math.hypot(north, east)


def _load_known_customers() -> list[dict[str, Any]]:
    try:
        payload = json.loads(FEEDBACK_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("kayitlar") if isinstance(payload, dict) else []
    return [
        dict(row)
        for row in (rows or [])
        if isinstance(row, dict)
        and str(row.get("sonuc") or "").strip().upper() == KNOWN_CUSTOMER_OUTCOME
    ]


def _matching_customer(
    candidate: dict[str, Any], known_customers: list[dict[str, Any]]
) -> tuple[dict[str, Any], float] | None:
    for record in known_customers:
        radius = _number(record.get("eslesme_yaricapi_m"), DEFAULT_CUSTOMER_RADIUS_M)
        radius = max(5.0, min(float(radius or DEFAULT_CUSTOMER_RADIUS_M), MAX_CUSTOMER_RADIUS_M))
        distance = _distance_m(candidate, record)
        if distance <= radius:
            return record, distance
    return None


def _is_low_priority_patrol(item: dict[str, Any]) -> bool:
    """Yalnız açık Gülbahçe işaretini bastır; karma doğu bölgesi etiketini kullanma."""
    if item.get("gulbahce_operasyonel_kapsama") is True:
        return True
    if item.get("gulbahce_cekirdek_operasyon") is True:
        return True
    for key in ("mahalle", "mahalle_yaklasik", "mevki"):
        if "gülbahçe" in _normalize(item.get(key)):
            return True
    return False


def _sanitize_field_report(text: str) -> tuple[str, int]:
    """Açık Gülbahçe saha yönlendirmelerini markdown'dan çıkarır.

    Yalnız kullanıcıya operasyon rotası gibi sunulan üç bölüm hedeflenir:
    günün ilk kontrolleri, kör-alan saha devriyesi ve saha adayları. Karma doğu
    bölgesi açıklamasında geçen ``Gülbahçe`` kelimesi tek başına eleme sebebi
    değildir; doğrudan aday/devriye satırı veya aday başlığı Gülbahçe olmalıdır.
    """
    lines = text.splitlines()
    output: list[str] = []
    section = ""
    skip_candidate_block = False
    skip_blind_item = False
    first3_index = 0
    blind_index = 0
    candidate_index = 0
    removed = 0

    for line in lines:
        if line.startswith("## "):
            section = _normalize(line)
            skip_candidate_block = False
            skip_blind_item = False
            first3_index = 0
            blind_index = 0
            candidate_index = 0
            output.append(line)
            continue

        if "günün ilk 3 kontrolü" in section:
            match = re.match(r"^(\d+)\.\s+(.*)$", line)
            if match:
                if "gülbahçe" in _normalize(match.group(2)):
                    removed += 1
                    continue
                first3_index += 1
                line = f"{first3_index}. {match.group(2)}"
            output.append(line)
            continue

        if "kör alan saha devriyesi" in section:
            match = re.match(r"^(\d+)\.\s+(\*\*KÖR ALAN\s+—\s+.*)$", line)
            if match:
                skip_blind_item = "gülbahçe" in _normalize(match.group(2))
                if skip_blind_item:
                    removed += 1
                    continue
                blind_index += 1
                line = f"{blind_index}. {match.group(2)}"
            elif skip_blind_item:
                # Kör alan maddesinin açıklama/boş satırlarını da bir sonraki
                # numaralı madde veya bölüm başlığına kadar göstermeyiz.
                if not re.match(r"^\d+\.\s+", line):
                    continue
                skip_blind_item = False
            output.append(line)
            continue

        if "bugün sahada kontrol edilecek uydu adayları" in section:
            heading = re.match(r"^###\s+\d+\.\s+(.*)$", line)
            if heading:
                skip_candidate_block = "gülbahçe" in _normalize(heading.group(1))
                if skip_candidate_block:
                    removed += 1
                    continue
                candidate_index += 1
                line = f"### {candidate_index}. {heading.group(1)}"
                output.append(line)
                continue
            if skip_candidate_block:
                continue
            output.append(line)
            continue

        output.append(line)

    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(output).rstrip("\n") + suffix, removed


def apply_focus(
    report: dict[str, Any], known_customers: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    known_customers = _load_known_customers() if known_customers is None else known_customers
    result = dict(report)
    candidates = []
    background = []

    for raw in report.get("saha_adaylari") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        reasons: list[str] = []

        if _normalize(item.get("mahalle")) in LOW_PRIORITY_NEIGHBORHOODS:
            item["rota_uygun"] = False
            item["operasyon_odak_disinda"] = True
            reasons.append(
                "Gülbahçe mevcut operasyon odağında günlük ekip rotasına alınmıyor; "
                "kayıt silinmeden arka planda izleniyor."
            )

        customer_match = _matching_customer(item, known_customers)
        if customer_match is not None:
            record, distance = customer_match
            item["rota_uygun"] = False
            item["mevcut_musteri"] = True
            item["mevcut_musteri_kayit_id"] = record.get("id")
            item["mevcut_musteri_mesafe_m"] = round(distance, 1)
            item["mevcut_musteri_notu"] = record.get("neden")
            # Bu sinyal yanlış pozitif değildir: model için gerçek yapılaşma referansı
            # olarak korunur, yalnız yeni müşteri saha rotasından çıkarılır.
            item["model_kalibrasyon_sinifi"] = "POZITIF_SANTIYE"
            reasons.append(
                "Konum mevcut müşteri sahası olarak doğrulandı; gerçek şantiye sinyali "
                "kalibrasyonda korunuyor fakat yeni müşteri kontrol rotasına alınmıyor."
            )

        if reasons:
            item["operasyon_odak_notu"] = " ".join(reasons)
            background.append(
                {
                    "gorev_id": item.get("gorev_id"),
                    "mahalle": item.get("mahalle"),
                    "enlem": item.get("enlem"),
                    "boylam": item.get("boylam"),
                    "alan_m2": item.get("alan_m2"),
                    "saha_durumu": item.get("saha_durumu"),
                    "operasyon_odak_disinda": bool(item.get("operasyon_odak_disinda")),
                    "mevcut_musteri": bool(item.get("mevcut_musteri")),
                    "mevcut_musteri_kayit_id": item.get("mevcut_musteri_kayit_id"),
                }
            )
        candidates.append(item)

    patrol = []
    patrol_background = []
    for raw in report.get("kor_alan_saha_devriyesi") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        if _is_low_priority_patrol(item):
            item["alarm"] = False
            item["saha_gorevi"] = False
            item["rota_uygun"] = False
            item["operasyon_odak_disinda"] = True
            item["operasyon_odak_notu"] = (
                "Gülbahçe operasyonel öncelik dışında; kör-alan gözlemi diagnostik "
                "arka planda korunur ve günlük saha rotasına gönderilmez."
            )
            patrol_background.append(item)
            continue
        patrol.append(item)

    result["saha_adaylari"] = candidates
    result["kor_alan_saha_devriyesi"] = patrol
    result["operasyon_odak_disinda_devriye"] = patrol_background
    result["operasyon_odak_disinda_devriye_sayi"] = len(patrol_background)
    result["operasyon_odak_arka_plan"] = background
    result["operasyon_odak_arka_plan_sayi"] = len(background)
    result["mevcut_musteri_arka_plan_sayi"] = sum(
        1 for row in background if row.get("mevcut_musteri")
    )
    result["operasyon_odak_notu"] = (
        "Günlük rota Çeşme yarımadası–Uzunkuyu odağındadır. Açıkça Gülbahçe etiketli "
        "adaylar ve doğrulanmış mevcut müşteri sahaları günlük yeni-müşteri rotasının "
        "dışında tutulur. Açık Gülbahçe kör-alan devriyeleri diagnostik arka planda "
        "korunur fakat saha rotasına girmez. Mevcut müşteri sinyali yanlış pozitif "
        "sayılmaz; model kalibrasyonunda gerçek şantiye olarak korunur. Doğrulanmamış "
        "mevki veya Gülbahçe kelimesi geçen karma doğu-bölgesi etiketi coğrafi tahminle "
        "elenmez."
    )
    return result


def _self_check() -> None:
    base = {"gorev_id": "A", "rota_uygun": True, "saha_durumu": "KONTROLE_GIT"}
    known = [
        {
            "id": "CUSTOMER-TEST",
            "sonuc": KNOWN_CUSTOMER_OUTCOME,
            "enlem": 38.286413,
            "boylam": 26.240205,
            "eslesme_yaricapi_m": 30,
            "neden": "test",
        }
    ]
    report = {
        "saha_adaylari": [
            {**base, "mahalle": "Gülbahçe", "enlem": 38.331547, "boylam": 26.644338},
            {
                **base,
                "gorev_id": "B",
                "mahalle": "Mevki doğrulanmadı",
                "enlem": 38.32015,
                "boylam": 26.562138,
            },
            {
                **base,
                "gorev_id": "C",
                "mahalle": "Çiftlikköy",
                "enlem": 38.286413,
                "boylam": 26.240205,
            },
            {
                **base,
                "gorev_id": "D",
                "mahalle": "Çiftlikköy",
                "enlem": 38.287413,
                "boylam": 26.240205,
            },
        ],
        "kor_alan_saha_devriyesi": [
            {
                "bolge_anahtari": "east",
                "bolge": "Uzunkuyu · Germiyan · Ildır · Gülbahçe",
                "mahalle": "Ildır",
                "enlem": 38.40,
                "boylam": 26.47,
                "saha_gorevi": False,
            },
            {
                "bolge_anahtari": "east",
                "bolge": "Uzunkuyu · Germiyan · Ildır · Gülbahçe",
                "mahalle": "Mevki doğrulanmadı",
                "enlem": 38.333,
                "boylam": 26.646,
                "gulbahce_operasyonel_kapsama": True,
                "saha_gorevi": False,
            },
        ],
    }
    result = apply_focus(report, known)
    rows = {row["gorev_id"]: row for row in result["saha_adaylari"]}
    assert rows["A"]["rota_uygun"] is False
    assert rows["B"]["rota_uygun"] is True
    assert rows["C"]["rota_uygun"] is False
    assert rows["C"]["mevcut_musteri"] is True
    assert rows["C"]["model_kalibrasyon_sinifi"] == "POZITIF_SANTIYE"
    assert rows["D"]["rota_uygun"] is True
    assert result["operasyon_odak_arka_plan_sayi"] == 2
    assert result["mevcut_musteri_arka_plan_sayi"] == 1
    # Karma doğu bölgesi etiketindeki Gülbahçe kelimesi Ildır devriyesini elememeli.
    assert len(result["kor_alan_saha_devriyesi"]) == 1
    assert result["kor_alan_saha_devriyesi"][0]["mahalle"] == "Ildır"
    # Açık Gülbahçe kapsama bayrağı operasyon rotasından ayrılmalı ama silinmemeli.
    assert result["operasyon_odak_disinda_devriye_sayi"] == 1
    assert result["operasyon_odak_disinda_devriye"][0]["rota_uygun"] is False

    sample_md = """# Şantiye Radarı

## Günün ilk 3 kontrolü

1. **PARSEL — Gülbahçe** · test
2. **PARSEL — Ovacık** · test

## Kör alan saha devriyesi

1. **KÖR ALAN — Musalla** · test
   - Saha notu: kalır
2. **KÖR ALAN — Gülbahçe · güncel uydu kör alanı** · test
   - Saha notu: çıkar

## Bugün sahada kontrol edilecek uydu adayları

### 1. TEKRAR — Gülbahçe
- **Yaklaşık konum:** Uzunkuyu · Germiyan · Ildır · Gülbahçe / Gülbahçe
- **Koordinat:** `38.33, 26.64`

### 2. NORMAL — Ildır
- **Yaklaşık konum:** Uzunkuyu · Germiyan · Ildır · Gülbahçe / Ildır
- **Koordinat:** `38.40, 26.47`
"""
    cleaned, removed = _sanitize_field_report(sample_md)
    assert removed == 3
    assert "PARSEL — Gülbahçe" not in cleaned
    assert "KÖR ALAN — Gülbahçe" not in cleaned
    assert "TEKRAR — Gülbahçe" not in cleaned
    assert "1. **PARSEL — Ovacık**" in cleaned
    assert "1. **KÖR ALAN — Musalla**" in cleaned
    assert "### 1. NORMAL — Ildır" in cleaned
    assert "Uzunkuyu · Germiyan · Ildır · Gülbahçe / Ildır" in cleaned


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.self_check:
        print("Operasyon odak koruması öz testi başarılı.")
        return

    if not REPORT_FILE.exists():
        raise RuntimeError("latest_report.json bulunamadı.")
    report = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise RuntimeError("latest_report.json nesne olmalıdır.")
    result = apply_focus(report)
    REPORT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    markdown_removed = 0
    if FIELD_REPORT_FILE.exists():
        original_md = FIELD_REPORT_FILE.read_text(encoding="utf-8")
        cleaned_md, markdown_removed = _sanitize_field_report(original_md)
        if cleaned_md != original_md:
            FIELD_REPORT_FILE.write_text(cleaned_md, encoding="utf-8")

    print(
        "Operasyon odak koruması: "
        f"arka_plan={result['operasyon_odak_arka_plan_sayi']}, "
        f"mevcut_musteri={result['mevcut_musteri_arka_plan_sayi']}, "
        f"devriye_arka_plan={result['operasyon_odak_disinda_devriye_sayi']}, "
        f"markdown_cikarilan={markdown_removed}"
    )


if __name__ == "__main__":
    main()
