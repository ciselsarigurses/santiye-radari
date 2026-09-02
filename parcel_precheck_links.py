"""Radar koordinatlarına manuel TKGM Parsel Sorgu ön-kontrol bağlantısı ekler.

Bu katman ada/parsel VERİSİ üretmez ve TKGM servisinden veri çekmez. Sentinel
koordinatı yaklaşık değişim merkezi olduğu için yalnız resmi Parsel Sorgu
arayüzünü aynı koordinatta açan bir bağlantı üretir. Ada/parsel kullanıcı
kontrolüyle doğrulanana kadar ``MANUEL_DOGRULAMA_GEREKLI`` kalır.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path


REPORT_JSON = Path(__file__).with_name("latest_report.json")
FIELD_REPORT_MD = Path(__file__).with_name("SAHA_RAPORU.md")
TKGM_BASE = "https://parselsorgu.tkgm.gov.tr/#ara/cografi"
PARCEL_STATUS = "MANUEL_DOGRULAMA_GEREKLI"
PARCEL_NOTE = (
    "Radar koordinatı yaklaşık değişim merkezidir; TKGM Parsel Sorgu haritasında "
    "manuel kontrol edilmelidir. Ada/parsel otomatik çıkarılmadı veya doğrulanmadı."
)
REPORT_NOTICE = (
    "> **Parsel ön kontrol:** Rota satırlarındaki **Parsel Sorgu'da aç** bağlantısı "
    "yalnız radar koordinatını TKGM haritasında açar; ada/parsel otomatik "
    "çıkarılmaz ve doğrulanmış kabul edilmez."
)
MAP_DESTINATION_RE = re.compile(
    r"https://www\.google\.com/maps/dir/\?api=1&destination="
    r"(?P<lat>-?\d+(?:\.\d+)?),(?P<lon>-?\d+(?:\.\d+)?)"
)


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _valid_point(latitude, longitude):
    return (
        latitude is not None
        and longitude is not None
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )


def parcel_query_url(latitude, longitude):
    """Resmi Parsel Sorgu arayüzünü koordinat aramasında açan URL'yi üret."""
    lat = _number(latitude)
    lon = _number(longitude)
    if not _valid_point(lat, lon):
        return None
    return f"{TKGM_BASE}/{lat:.6f}/{lon:.6f}"


def _enrich_coordinates(node):
    """JSON içindeki koordinatlı sözlüklere yalnız manuel ön-kontrol metadatası ekle."""
    changed = 0
    if isinstance(node, dict):
        url = parcel_query_url(node.get("enlem"), node.get("boylam"))
        if url:
            desired = {
                "parsel_sorgu": url,
                "ada_parsel_durumu": PARCEL_STATUS,
                "parsel_notu": PARCEL_NOTE,
            }
            for key, value in desired.items():
                if node.get(key) != value:
                    node[key] = value
                    changed += 1
        for value in list(node.values()):
            changed += _enrich_coordinates(value)
    elif isinstance(node, list):
        for value in node:
            changed += _enrich_coordinates(value)
    return changed


def _enrich_markdown(text):
    """Mevcut Google rota satırlarına idempotent TKGM manuel ön-kontrol linki ekle."""
    source = str(text or "")
    lines = source.splitlines()
    output = []
    notice_present = REPORT_NOTICE in source
    notice_inserted = notice_present

    for line in lines:
        output.append(line)
        if (
            not notice_inserted
            and line.startswith("> **Konum kuralı:**")
        ):
            output.extend(["", REPORT_NOTICE])
            notice_inserted = True

        if "parselsorgu.tkgm.gov.tr" in line:
            continue
        match = MAP_DESTINATION_RE.search(line)
        if not match:
            continue
        url = parcel_query_url(match.group("lat"), match.group("lon"))
        if not url:
            continue
        output[-1] = line + f" · [Parsel Sorgu'da aç]({url})"

    if not notice_inserted:
        prefix = [REPORT_NOTICE, ""]
        output = prefix + output

    result = "\n".join(output)
    if source.endswith("\n"):
        result += "\n"
    return result


def update_report_files():
    json_changes = 0
    markdown_changed = False

    if REPORT_JSON.exists():
        original = REPORT_JSON.read_text(encoding="utf-8")
        payload = json.loads(original)
        json_changes = _enrich_coordinates(payload)
        payload["parsel_on_kontrol_notu"] = PARCEL_NOTE
        rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if rendered != original:
            REPORT_JSON.write_text(rendered, encoding="utf-8")

    if FIELD_REPORT_MD.exists():
        original_md = FIELD_REPORT_MD.read_text(encoding="utf-8")
        updated_md = _enrich_markdown(original_md)
        if updated_md != original_md:
            FIELD_REPORT_MD.write_text(updated_md, encoding="utf-8")
            markdown_changed = True

    print(
        "Parsel ön kontrol: "
        f"JSON metadata değişikliği={json_changes}, markdown_değişti={markdown_changed}"
    )


def _self_check():
    assert parcel_query_url(38.338783, 26.311638) == (
        "https://parselsorgu.tkgm.gov.tr/#ara/cografi/38.338783/26.311638"
    )
    assert parcel_query_url("x", 26.3) is None
    assert parcel_query_url(91, 26.3) is None

    sample = {
        "saha_adaylari": [
            {"enlem": 38.338783, "boylam": 26.311638, "alan_m2": 400},
            {"enlem": None, "boylam": 26.3},
        ]
    }
    changes = _enrich_coordinates(sample)
    assert changes == 3
    item = sample["saha_adaylari"][0]
    assert item["ada_parsel_durumu"] == PARCEL_STATUS
    assert item["parsel_sorgu"].endswith("/38.338783/26.311638")
    assert _enrich_coordinates(sample) == 0

    markdown = (
        "> **Konum kuralı:** Test.\n\n"
        "1. **KONTROL — Dalyan** · "
        "[Yol tarifi](https://www.google.com/maps/dir/?api=1&destination="
        "38.338783,26.311638)\n"
    )
    once = _enrich_markdown(markdown)
    twice = _enrich_markdown(once)
    assert once == twice
    assert once.count("parselsorgu.tkgm.gov.tr") == 1
    assert REPORT_NOTICE in once
    print("parcel_precheck_links self-check: OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        _self_check()
        return
    update_report_files()


if __name__ == "__main__":
    main()
