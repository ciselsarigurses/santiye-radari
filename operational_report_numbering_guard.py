"""Saha raporundaki operasyon sırasını rapor mimarisine göre doğrular.

Eski günlük rapor biçiminde ``latest_report.json`` içindeki ``rota_uygun=true``
adaylar ``## Bugün sahada kontrol edilecek uydu adayları`` bölümüne yazılır; geniş
yüzey korumaları bazı blokları kaldırdığında bu bölümdeki ``### N.`` başlıkları
1..N olarak yeniden numaralandırılır.

Yeni mimaride ise nihai operasyon kaynağı ``operational_route.json`` ve kullanıcıya
sunulan bölüm ``## Günün ilk 3 kontrolü``dür. Bu durumda ham ``latest_report.json``
aday adediyle modern saha raporunu karşılaştırmak doğru değildir: genel Sentinel
adayları diagnostik havuzda kalabilirken nihai rota çoklu-kanıt kapılarından sonra
1-3 kayda düşebilir. Modern bölüm varsa bu dosya nihai rota sidecar'ını kaynak kabul
eder; eşzamanlı workflow sırası yüzünden markdown geçici olarak eski kalmışsa bölümü
aynı authoritative renderer ile deterministik biçimde senkronize eder ve sonra
fail-closed sayı doğrulaması yapar.

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


def _final_route_items():
    """Modern raporda tek kaynak olan nihai operasyon sidecar'ını doğrulayarak döndür."""
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
    return [dict(item) for item in items]


def _final_route_count():
    return len(_final_route_items())


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


def sync_modern_operational_section(text, route_items):
    """Modern bölümü nihai rotadan authoritative renderer ile yeniden kur.

    Workflow zincirleri aynı raporu farklı sıralarda yazabildiği için markdown bazen
    birkaç saniyeliğine eski rota sayısını taşıyabilir. Bu durumda hata vermek yerine
    yalnız modern operasyon bölümünü ``foundation_excavation_operational_route_guard``
    renderer'ıyla yeniden üretiriz; ardından sayı yine uyuşmuyorsa fail-closed kalırız.
    """
    from foundation_excavation_operational_route_guard import _update_markdown

    rendered = _update_markdown(str(text or ""), route_items)
    actual, found = count_modern_operational_section(rendered.splitlines())
    expected = len(route_items)
    if not found or actual != expected:
        raise RuntimeError(
            "Modern operasyon markdown senkronizasyonu nihai rotayı üretemedi: "
            f"markdown={actual}, final_rota={expected}"
        )
    return rendered


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

    # Modern markdown eski rota sayısını taşısa bile nihai sidecar tek kaynak olmalı.
    repaired = sync_modern_operational_section(
        "\n".join(modern) + "\n",
        [
            {
                "oncelik": "YUKSEK",
                "mahalle": "A",
                "alan_m2": 400,
                "gorev_id": "A1",
                "harita": "https://example.com/a",
            }
        ],
    )
    repaired_count, repaired_found = count_modern_operational_section(repaired.splitlines())
    assert repaired_found and repaired_count == 1
    assert "Görev `A1`" in repaired
    assert "2. **TEKRAR — B**" not in repaired
    assert "## Başka bölüm" in repaired


def apply_guard():
    _self_check()
    if not REPORT_MD.exists():
        raise RuntimeError("SAHA_RAPORU.md yok")

    original = REPORT_MD.read_text(encoding="utf-8")
    lines = original.splitlines()

    modern_actual, modern_found = count_modern_operational_section(lines)
    if modern_found:
        final_route = _final_route_items()
        modern_expected = len(final_route)
        if modern_actual != modern_expected:
            rendered = sync_modern_operational_section(original, final_route)
            changed = rendered != original
            if changed:
                REPORT_MD.write_text(rendered, encoding="utf-8")
            repaired_actual, repaired_found = count_modern_operational_section(rendered.splitlines())
            if not repaired_found or repaired_actual != modern_expected:
                raise RuntimeError(
                    "Modern operasyon markdown onarım sonrası nihai rotayla uyuşmuyor: "
                    f"markdown={repaired_actual}, final_rota={modern_expected}"
                )
            return {
                "durum": "modern_final_rota_senkronize",
                "aktif": repaired_actual,
                "degisti": changed,
            }
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
            "modern Günün ilk 3 bölümü nihai operational_route.json ile doğrulanıp gerekirse senkronize ediliyor."
        )
        return
    result = apply_guard()
    if result["durum"] in {"modern_final_rota_dogrulandi", "modern_final_rota_senkronize"}:
        action = "senkronize edildi" if result["durum"] == "modern_final_rota_senkronize" else "doğrulandı"
        print(
            f"Saha raporu sıra koruması: modern nihai rota {action}; "
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
