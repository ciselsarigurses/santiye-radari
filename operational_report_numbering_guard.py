"""Saha raporundaki operasyon adaylarını filtre sonrası yeniden numaralandırır.

Geniş-yüzey ve benzeri sunum korumaları SAHA_RAPORU.md içinden bazı operasyon
bloklarını güvenli biçimde çıkarabilir. Markdown başlıklarındaki eski sıra numaraları
ise yerinde kalırsa özet 99 aktif görev derken son başlık 105 görünebilir. Bu katman
algılama, alarm, görev, Sentinel eşiği veya SQLite verisini değiştirmez; yalnız
"Bugün sahada kontrol edilecek uydu adayları" bölümündeki kalan `### N.` başlıklarını
1..N olarak yeniden numaralandırır ve blok sayısının latest_report.json içindeki
`saha_adaylari` sayısıyla aynı olduğunu doğrular.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPORT_JSON = ROOT / "latest_report.json"
REPORT_MD = ROOT / "SAHA_RAPORU.md"
SECTION_TITLE = "## Bugün sahada kontrol edilecek uydu adayları"
HEADING_RE = re.compile(r"^(###\s+)\d+(\.\s+.+)$")


def _active_count():
    if not REPORT_JSON.exists():
        raise RuntimeError("latest_report.json yok")
    try:
        payload = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"latest_report.json okunamadı: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("latest_report.json nesne değil")
    items = payload.get("saha_adaylari") or []
    if not isinstance(items, list):
        raise RuntimeError("saha_adaylari liste değil")
    return sum(isinstance(item, dict) for item in items)


def renumber_operational_section(lines):
    output = []
    in_section = False
    found_section = False
    count = 0

    for line in lines:
        if line.strip() == SECTION_TITLE:
            in_section = True
            found_section = True
            output.append(line)
            continue
        if in_section and line.startswith("## "):
            in_section = False
        if in_section:
            match = HEADING_RE.match(line)
            if match:
                count += 1
                line = f"{match.group(1)}{count}{match.group(2)}"
        output.append(line)

    return output, count, found_section


def _self_check():
    sample = [
        "# Rapor",
        "## Bugün sahada kontrol edilecek uydu adayları",
        "",
        "### 1. ERKEN — A",
        "- **Koordinat:** `38.1, 26.1`",
        "",
        "### 7. ORTA — B",
        "- **Koordinat:** `38.2, 26.2`",
        "",
        "### 105. BEKLEYEN — C",
        "- **Koordinat:** `38.3, 26.3`",
        "",
        "## Arka planda izlenen geniş yüzey hareketleri",
        "### 44. Bu başlık operasyon bölümü dışında",
    ]
    updated, count, found = renumber_operational_section(sample)
    text = "\n".join(updated)
    assert found and count == 3
    assert "### 1. ERKEN — A" in text
    assert "### 2. ORTA — B" in text
    assert "### 3. BEKLEYEN — C" in text
    assert "### 44. Bu başlık operasyon bölümü dışında" in text
    assert "### 105. BEKLEYEN — C" not in text


def apply_guard():
    _self_check()
    if not REPORT_MD.exists():
        raise RuntimeError("SAHA_RAPORU.md yok")

    expected = _active_count()
    original = REPORT_MD.read_text(encoding="utf-8")
    lines = original.splitlines()
    updated, actual, found = renumber_operational_section(lines)

    if not found:
        if expected == 0:
            return {"durum": "bolum_yok_aktif_yok", "aktif": 0, "degisti": False}
        raise RuntimeError(
            f"Operasyon bölümü yok ama latest_report.json {expected} aktif aday içeriyor"
        )
    if actual != expected:
        raise RuntimeError(
            "Operasyon markdown blok sayısı latest_report.json ile uyuşmuyor: "
            f"markdown={actual}, json={expected}"
        )

    rendered = "\n".join(updated)
    if original.endswith("\n"):
        rendered += "\n"
    changed = rendered != original
    if changed:
        REPORT_MD.write_text(rendered, encoding="utf-8")
    return {"durum": "ok", "aktif": actual, "degisti": changed}


def main(check_only=False):
    _self_check()
    if check_only:
        print(
            "Saha raporu sıra koruması öz testi başarılı: yalnız operasyon markdown "
            "başlıklarını yeniden numaralandırıyor."
        )
        return
    result = apply_guard()
    print(
        "Saha raporu sıra koruması: "
        f"{result['aktif']} aktif blok; "
        + ("numaralar düzeltildi." if result["degisti"] else "numaralar zaten tutarlı.")
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    main(check_only=args.check_only)
