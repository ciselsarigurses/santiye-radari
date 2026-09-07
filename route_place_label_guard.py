"""Eski uydu görevlerindeki operasyonel mevki etiketini koordinatla tutarlı tutar.

Bu katman yalnız rapor sunumunu düzeltir. Görev koordinatı, kimliği, alarm/saha
durumu, Sentinel eşikleri, adres, ada, parsel veya hukuki statü değiştirilmez.
Yalnız U* uydu görevlerinde mevcut etiket kendi operasyonel referans merkezinden
3 km'den daha uzaktaysa ve başka tanımlı referans merkez 3 km içinde açıkça daha
yakınsa etiket düzeltilir. Bu etiket idari/kadastral sınır iddiası değildir.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

from satellite import MAX_PLACE_LABEL_DISTANCE_M, PLACE_CENTERS


REPORT_JSON = Path(__file__).with_name("latest_report.json")
FIELD_REPORT_MD = Path(__file__).with_name("SAHA_RAPORU.md")
REPORT_LIST_KEYS = (
    "saha_adaylari",
    "gunun_ilk_3_kontrolu",
)
OPERATIONAL_LABEL_SOURCE = "KOORDINAT_YAKIN_MERKEZ"
OPERATIONAL_LABEL_NOTE = (
    "Operasyonel yakın-merkez etiketi; idari/kadastral sınır veya ada/parsel değildir."
)


def _number(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _distance_m(latitude, longitude, center):
    center_latitude, center_longitude = center
    mean_latitude = math.radians((latitude + center_latitude) / 2)
    north = (latitude - center_latitude) * 110570
    east = (longitude - center_longitude) * 111320 * math.cos(mean_latitude)
    return float(math.hypot(north, east))


def _nearest_reference(latitude, longitude):
    ranked = sorted(
        (
            (_distance_m(latitude, longitude, center), name)
            for name, center in PLACE_CENTERS.items()
        ),
        key=lambda pair: (pair[0], pair[1]),
    )
    return ranked[0] if ranked else (None, None)


def corrected_operational_label(item):
    """Güvenli bir stale-label düzeltmesi varsa yeni etiketi döndür."""
    if not isinstance(item, dict):
        return None
    task_id = str(item.get("gorev_id") or "").strip().upper()
    if not task_id.startswith("U"):
        return None

    current = str(item.get("mahalle") or "").strip()
    if current not in PLACE_CENTERS:
        return None

    latitude = _number(item.get("enlem"))
    longitude = _number(item.get("boylam"))
    if latitude is None or longitude is None:
        return None

    current_distance = _distance_m(latitude, longitude, PLACE_CENTERS[current])
    if current_distance <= MAX_PLACE_LABEL_DISTANCE_M:
        return None

    nearest_distance, nearest = _nearest_reference(latitude, longitude)
    if (
        nearest is None
        or nearest == current
        or nearest_distance is None
        or nearest_distance > MAX_PLACE_LABEL_DISTANCE_M
    ):
        return None
    return nearest


def _coordinate_key(item):
    latitude = _number(item.get("enlem"))
    longitude = _number(item.get("boylam"))
    if latitude is None or longitude is None:
        return None
    return round(latitude, 6), round(longitude, 6)


def normalize_report(payload):
    """Rapor içindeki güvenli stale mevki etiketlerini düzelt."""
    if not isinstance(payload, dict):
        return {}, []

    corrections = {}
    changed_rows = []
    for key in REPORT_LIST_KEYS:
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            new_label = corrected_operational_label(item)
            if not new_label:
                continue
            old_label = str(item.get("mahalle") or "").strip()
            coordinate = _coordinate_key(item)
            item["mahalle"] = new_label
            item["mahalle_kaynagi"] = OPERATIONAL_LABEL_SOURCE
            item["mahalle_notu"] = OPERATIONAL_LABEL_NOTE
            if coordinate:
                corrections[coordinate] = (old_label, new_label)
            changed_rows.append(
                {
                    "liste": key,
                    "gorev_id": str(item.get("gorev_id") or ""),
                    "eski": old_label,
                    "yeni": new_label,
                    "koordinat": coordinate,
                }
            )
    return corrections, changed_rows


def _block_coordinate(block):
    match = re.search(
        r"\*\*Koordinat:\*\*\s*`\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*`",
        block,
    )
    if not match:
        return None
    return round(float(match.group(1)), 6), round(float(match.group(2)), 6)


def normalize_markdown(text, corrections):
    """Yalnız koordinatı eşleşen aday bloğunda eski operasyonel etiketi değiştir."""
    if not text or not corrections:
        return text

    block_pattern = re.compile(r"(?ms)^### .+?(?=^### |^## |\Z)")

    def replace_block(match):
        block = match.group(0)
        coordinate = _block_coordinate(block)
        if coordinate not in corrections:
            return block
        old_label, new_label = corrections[coordinate]
        if old_label == new_label:
            return block
        lines = block.splitlines()
        for index, line in enumerate(lines):
            if index == 0 and line.rstrip().endswith(f"— {old_label}"):
                lines[index] = line[: -len(old_label)] + new_label
            elif line.startswith("- **Yaklaşık konum:**") and line.rstrip().endswith(
                f"/ {old_label}"
            ):
                lines[index] = line[: -len(old_label)] + new_label
        return "\n".join(lines) + ("\n" if block.endswith("\n") else "")

    return block_pattern.sub(replace_block, text)


def _self_check():
    stale = {
        "gorev_id": "U1CA0ACAC42",
        "mahalle": "Uzunkuyu",
        "enlem": 38.33219,
        "boylam": 26.652385,
        "adres": None,
        "ada": None,
        "parsel": None,
    }
    assert corrected_operational_label(stale) == "Gülbahçe"

    site = dict(stale, gorev_id="S123")
    assert corrected_operational_label(site) is None

    unknown = dict(stale, mahalle="Bilinmeyen")
    assert corrected_operational_label(unknown) is None

    already_plausible = dict(stale, mahalle="Gülbahçe")
    assert corrected_operational_label(already_plausible) is None

    payload = {"saha_adaylari": [dict(stale)]}
    corrections, rows = normalize_report(payload)
    assert rows and payload["saha_adaylari"][0]["mahalle"] == "Gülbahçe"
    assert payload["saha_adaylari"][0]["adres"] is None
    assert payload["saha_adaylari"][0]["ada"] is None
    assert payload["saha_adaylari"][0]["parsel"] is None

    markdown = (
        "## Bugün sahada kontrol edilecek uydu adayları\n\n"
        "### 8. GECİKEN — Uzunkuyu\n"
        "- **Yaklaşık konum:** Uzunkuyu · Germiyan · Ildır · Gülbahçe / Uzunkuyu\n"
        "- **Koordinat:** `38.33219, 26.652385`\n"
        "- **Değişim alanı:** yaklaşık 0 m²\n"
    )
    corrected = normalize_markdown(markdown, corrections)
    assert "### 8. GECİKEN — Gülbahçe" in corrected
    assert "/ Gülbahçe" in corrected
    assert "38.33219, 26.652385" in corrected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.check_only:
        print("Uydu mevki etiketi koordinat koruması öz testi başarılı.")
        return

    if not REPORT_JSON.exists():
        print("latest_report.json yok; düzeltilecek rapor bulunamadı.")
        return

    payload = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    corrections, changed_rows = normalize_report(payload)
    if not changed_rows:
        print("Koordinatla çelişen güvenli-düzeltilebilir uydu mevki etiketi yok.")
        return

    REPORT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if FIELD_REPORT_MD.exists():
        current_markdown = FIELD_REPORT_MD.read_text(encoding="utf-8")
        updated_markdown = normalize_markdown(current_markdown, corrections)
        if updated_markdown != current_markdown:
            FIELD_REPORT_MD.write_text(updated_markdown, encoding="utf-8")

    for row in changed_rows:
        print(
            f"{row['gorev_id']}: {row['eski']} -> {row['yeni']} "
            f"@ {row['koordinat']}"
        )
    print(f"Toplam {len(changed_rows)} rapor satırı güvenli biçimde düzeltildi.")


if __name__ == "__main__":
    main()
