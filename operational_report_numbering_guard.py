"""Saha raporundaki operasyon sırasını rapor mimarisine göre doğrular.

Eski günlük rapor biçiminde ``latest_report.json`` içindeki ``rota_uygun=true``
adaylar ``## Bugün sahada kontrol edilecek uydu adayları`` bölümüne yazılır; geniş
yüzey korumaları bazı blokları kaldırdığında bu bölümdeki ``### N.`` başlıkları
1..N olarak yeniden numaralandırılır.

Yeni mimaride ise nihai operasyon kaynağı ``operational_route.json`` ve kullanıcıya
sunulan bölüm ``## Günün ilk 3 kontrolü``dür. Bu durumda ham ``latest_report.json``
aday adediyle modern saha raporunu karşılaştırmak doğru değildir: genel Sentinel
adayları diagnostik havuzda kalabilirken nihai rota çoklu-kanıt kapılarından sonra
1-3 kayda düşebilir. Modern bölüm varsa bu dosya artık nihai rota sidecar'ıyla
fail-closed biçimde sayı tutarlılığı doğrular ve legacy başlık numaralandırmasını
uygulamaz.

Bu katman algılama, alarm, görev, Sentinel eşiği veya SQLite verisini değiştirmez.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPORT_JSON = ROOT / "latest_report.json"
REPORT_MD = ROOT / "SAHA_RAPORU.md"
OPERATIONAL_ROUTE_JSON = ROOT / "operational_route.json"
LEGACY_SECTION_TITLE = "## Bugün sahada kontrol edilecek uydu adayları"
MODERN_SECTION_TITLE = "## Günün ilk 3 kontrolü"
HEADING_RE = re.compile(r"^(###\s+)\d+(\.\s+.+)$")
MODERN_ENTRY_RE = re.compile(r"^\d+\.\s+\*\*")


def _operational_count(items):
    """daily_report.py ile aynı rota filtresini kullanarak legacy operasyon adedini döndür."""
    return sum(
        isinstance(item, dict) and bool(item.get("rota_uygun", True))
        for item in items
    )


def _legacy_active_count():
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
    return _operational_count(items)


def _final_route_count():
    """Modern raporda tek kaynak olan nihai operasyon sidecar'ının kayıt sayısı."""
    if not OPERATIONAL_ROUTE_JSON.exists():
        raise RuntimeError("operational_route.json yok")
    try:
        payload = json.loads(OPERATIONAL_ROUTE_JSON.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"operational_route.json okunamadı: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("operational_route.json nesne değil")
    items = payload.get("operasyonel_rota") or []
    if not isinstance(items, list):
        raise RuntimeError("operasyonel_rota liste değil")
    if any(not isinstance(item, dict) for item in items):
        raise RuntimeError("operasyonel_rota içinde nesne olmayan kayıt var")
    return len(items)


def renumber_legacy_operational_section(lines):
    output = []
    in_section = False
    found_section = False
    count = 0

    for line in lines:
        if line.strip() == LEGACY_SECTION_TITLE:
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


def count_modern_operational_section(lines):
    in_section = False
    found_section = False
    count = 0
    for line in lines:
        if line.strip() == MODERN_SECTION_TITLE:
            in_section = True
            found_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section and MODERN_ENTRY_RE.match(line.strip()):
            count += 1
    return count, found_section


def _self_check():
    legacy = [
        "# Rapor",
        LEGACY_SECTION_TITLE,
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
    updated, count, found = renumber_legacy_operational_section(legacy)
    text = "\n".join(updated)
    assert found and count == 3
    assert "### 1. ERKEN — A" in text
    assert "### 2. ORTA — B" in text
    assert "### 3. BEKLEYEN — C" in text
    assert "### 44. Bu başlık operasyon bölümü dışında" in text
    assert "### 105. BEKLEYEN — C" not in text
    assert _operational_count(
        [
            {"rota_uygun": True},
            {"rota_uygun": False},
            {},
            "gecersiz",
        ]
    ) == 2

    modern = [
        "# Rapor",
        MODERN_SECTION_TITLE,
        "",
        "> Çoklu-kanıt kapısı aktif.",
        "",
        "1. **YUKSEK — A** · yaklaşık 400 m²",
        "2. **TEKRAR — B** · yaklaşık 300 m²",
        "",
        "## Başka bölüm",
        "1. **Bu kayıt sayılmamalı**",
    ]
    modern_count, modern_found = count_modern_operational_section(modern)
    assert modern_found and modern_count == 2


def apply_guard():
    _self_check()
    if not REPORT_MD.exists():
        raise RuntimeError("SAHA_RAPORU.md yok")

    original = REPORT_MD.read_text(encoding="utf-8")
    lines = original.splitlines()

    modern_actual, modern_found = count_modern_operational_section(lines)
    if modern_found:
        modern_expected = _final_route_count()
        if modern_actual != modern_expected:
            raise RuntimeError(
                "Modern operasyon markdown sayısı operational_route.json nihai rotasıyla uyuşmuyor: "
                f"markdown={modern_actual}, final_rota={modern_expected}"
            )
        return {
            "durum": "modern_final_rota_dogrulandi",
            "aktif": modern_actual,
            "degisti": False,
        }

    expected = _legacy_active_count()
    updated, actual, found = renumber_legacy_operational_section(lines)

    if not found:
        # Modern ve legacy bölümün ikisi de yoksa yalnız gerçekten boş final rota
        # güvenli no-op sayılır. Nihai rota doluyken rapor bölümü kaybolmuşsa hata ver.
        final_expected = _final_route_count()
        if expected == 0 and final_expected == 0:
            return {"durum": "bolum_yok_aktif_yok", "aktif": 0, "degisti": False}
        raise RuntimeError(
            "Operasyon bölümü yok: "
            f"legacy_json_rota={expected}, final_rota={final_expected}"
        )
    if actual != expected:
        raise RuntimeError(
            "Legacy operasyon markdown blok sayısı latest_report.json rota adaylarıyla uyuşmuyor: "
            f"markdown={actual}, json_rota={expected}"
        )

    rendered = "\n".join(updated)
    if original.endswith("\n"):
        rendered += "\n"
    changed = rendered != original
    if changed:
        REPORT_MD.write_text(rendered, encoding="utf-8")
    return {"durum": "legacy_ok", "aktif": actual, "degisti": changed}


def main(check_only=False):
    _self_check()
    if check_only:
        print(
            "Saha raporu sıra koruması öz testi başarılı: legacy bölüm yeniden numaralanıyor; "
            "modern Günün ilk 3 bölümü nihai operational_route.json ile doğrulanıyor."
        )
        return
    result = apply_guard()
    if result["durum"] == "modern_final_rota_dogrulandi":
        print(
            "Saha raporu sıra koruması: modern nihai rota doğrulandı; "
            f"{result['aktif']} operasyon kaydı."
        )
        return
    print(
        "Saha raporu sıra koruması: "
        f"{result['aktif']} rota-uygun aktif blok; "
        + ("numaralar düzeltildi." if result["degisti"] else "numaralar zaten tutarlı.")
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    main(check_only=args.check_only)
