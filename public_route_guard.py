"""JSON'da rota dışı bırakılan adayların kullanıcıya açık saha raporuna sızmasını engeller.

Bu katman alarm/görev üretmez veya silmez. ``latest_report.json`` içindeki
``rota_uygun=False`` kararını ``SAHA_RAPORU.md`` görünümüne taşır. Böylece saha
geri bildirimiyle yanlış pozitif, tarla/bahçe veya mevcut müşteri olarak bastırılan
bir aday, daha eski markdown üretimi yüzünden yeniden "gidilecek yer" gibi görünmez.

250 m² ana eşik ve 150–249 m² MİKRO politikası değişmez.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parent
REPORT_FILE = BASE / "latest_report.json"
FIELD_REPORT_FILE = BASE / "SAHA_RAPORU.md"


def _point(item: dict[str, Any]) -> tuple[float, float] | None:
    try:
        return float(item.get("enlem")), float(item.get("boylam"))
    except (TypeError, ValueError, AttributeError):
        return None


def _blocked_markers(report: dict[str, Any]) -> tuple[set[str], set[str]]:
    task_ids: set[str] = set()
    points: set[str] = set()
    for raw in report.get("saha_adaylari") or []:
        if not isinstance(raw, dict) or bool(raw.get("rota_uygun", True)):
            continue
        task_id = str(raw.get("gorev_id") or "").strip()
        if task_id:
            task_ids.add(task_id)
        point = _point(raw)
        if point is not None:
            points.add(f"{point[0]:.6f}, {point[1]:.6f}")
            # Bazı eski raporlar yuvarlanmış koordinat yazmış olabilir.
            points.add(f"{point[0]}, {point[1]}")
    return task_ids, points


def _contains_blocked_marker(text: str, task_ids: set[str], points: set[str]) -> bool:
    return any(task_id in text for task_id in task_ids) or any(point in text for point in points)


def _renumber_first_controls(lines: list[str], task_ids: set[str], points: set[str]):
    kept: list[str] = []
    counter = 0
    removed = 0
    for line in lines:
        match = re.match(r"^(\d+)\.\s+(.*)$", line)
        if not match:
            kept.append(line)
            continue
        if _contains_blocked_marker(line, task_ids, points):
            removed += 1
            continue
        counter += 1
        kept.append(f"{counter}. {match.group(2)}")
    return kept, removed


def _renumber_candidate_blocks(lines: list[str], task_ids: set[str], points: set[str]):
    prefix: list[str] = []
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if re.match(r"^###\s+\d+\.\s+", line):
            if current is not None:
                blocks.append(current)
            current = [line]
        elif current is None:
            prefix.append(line)
        else:
            current.append(line)
    if current is not None:
        blocks.append(current)

    kept_blocks: list[list[str]] = []
    removed = 0
    for block in blocks:
        text = "\n".join(block)
        if _contains_blocked_marker(text, task_ids, points):
            removed += 1
            continue
        kept_blocks.append(block)

    output = list(prefix)
    for index, block in enumerate(kept_blocks, start=1):
        heading = re.sub(r"^###\s+\d+\.\s+", f"### {index}. ", block[0])
        output.extend([heading, *block[1:]])
    return output, removed


def sanitize_field_report(
    text: str, task_ids: set[str], points: set[str]
) -> tuple[str, int]:
    """Yalnız rota bölümlerini temizler; arşiv/diagnostik metinlerine dokunmaz."""
    lines = text.splitlines()
    output: list[str] = []
    removed = 0
    index = 0

    while index < len(lines):
        line = lines[index]
        if not line.startswith("## "):
            output.append(line)
            index += 1
            continue

        section_start = index
        index += 1
        while index < len(lines) and not lines[index].startswith("## "):
            index += 1
        section = lines[section_start:index]
        title = section[0].casefold()
        body = section[1:]

        if "günün ilk 3 kontrolü" in title:
            body, count = _renumber_first_controls(body, task_ids, points)
            removed += count
        elif "bugün sahada kontrol edilecek uydu adayları" in title:
            body, count = _renumber_candidate_blocks(body, task_ids, points)
            removed += count

        output.extend([section[0], *body])

    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(output).rstrip("\n") + suffix, removed


def _self_check() -> None:
    report = {
        "saha_adaylari": [
            {
                "gorev_id": "UFP001",
                "enlem": 38.313186,
                "boylam": 26.307059,
                "rota_uygun": False,
            },
            {
                "gorev_id": "UOK002",
                "enlem": 38.318703,
                "boylam": 26.387192,
                "rota_uygun": True,
            },
        ]
    }
    task_ids, points = _blocked_markers(report)
    sample = """# Şantiye Radarı

## Günün ilk 3 kontrolü

1. **ERKEN — Musalla** · Görev `UFP001`
2. **ERKEN — Ilıca** · Görev `UOK002`

## Bugün sahada kontrol edilecek uydu adayları

### 1. GECİKEN — Musalla
- **Koordinat:** `38.313186, 26.307059`
- test

### 2. ERKEN — Ilıca
- **Koordinat:** `38.318703, 26.387192`
- test

## Arşiv

- UFP001 burada kalabilir.
"""
    cleaned, removed = sanitize_field_report(sample, task_ids, points)
    assert removed == 2
    assert "GECİKEN — Musalla" not in cleaned
    assert "**ERKEN — Musalla**" not in cleaned
    assert "1. **ERKEN — Ilıca**" in cleaned
    assert "### 1. ERKEN — Ilıca" in cleaned
    assert "UFP001 burada kalabilir" in cleaned


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    _self_check()
    if args.self_check:
        print("public route guard self-check: OK")
        return

    if not REPORT_FILE.exists() or not FIELD_REPORT_FILE.exists():
        raise RuntimeError("latest_report.json veya SAHA_RAPORU.md bulunamadı")
    report = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise RuntimeError("latest_report.json nesne olmalıdır")
    text = FIELD_REPORT_FILE.read_text(encoding="utf-8")
    task_ids, points = _blocked_markers(report)
    cleaned, removed = sanitize_field_report(text, task_ids, points)
    if cleaned != text:
        FIELD_REPORT_FILE.write_text(cleaned, encoding="utf-8")
    print(
        "public route guard: "
        f"rota_disinda={len(task_ids)} · markdown_cikarilan={removed}"
    )


if __name__ == "__main__":
    main()
