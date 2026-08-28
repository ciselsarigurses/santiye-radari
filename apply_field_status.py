"""GitHub Issue başlığından bir saha görevinin durumunu ve sonucunu uygular."""

from __future__ import annotations

import re
import sys

from field_outcome import clear_outcome, save_outcome
from field_state import apply_status
from report_quality import normalize_daily_report


TITLE_PATTERN = re.compile(
    r"^\[SAHA\]\s+([SU][A-Z0-9]+)\s+"
    r"(KONTROLE_GIT|TEKRAR_GIT|KONTROL_EDILDI)"
    r"(?:\s+(SANTIYE_KAZI|YOL_ALTYAPI|TARLA_BITKI|YANLIS_POZITIF))?$"
)


def apply_issue_title(title):
    match = TITLE_PATTERN.fullmatch(str(title or "").strip().upper())
    if not match:
        raise ValueError(
            "Geçersiz saha talebi. Beklenen: [SAHA] <görev> <durum> [sonuç]"
        )
    task_id, status, outcome = match.groups()
    if outcome and status != "KONTROL_EDILDI":
        raise ValueError("Saha sonucu yalnızca KONTROL_EDILDI durumuyla kaydedilebilir.")

    result = apply_status(task_id, status)
    if outcome:
        result["sonuc"] = save_outcome(task_id, outcome)
    elif status != "KONTROL_EDILDI":
        clear_outcome(task_id)

    report = normalize_daily_report()
    return result, report


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        raise SystemExit("Kullanım: python apply_field_status.py '<issue başlığı>'")
    result, report = apply_issue_title(argv[0])
    message = (
        f"{result['gorev_id']}: {result['eski_durum']} -> "
        f"{result['yeni_durum']}"
    )
    if result.get("sonuc"):
        message += f" · sonuç={result['sonuc']}"
    print(message)
    if report:
        print(f"Günlük rapor yenilendi: {report['date']}")


if __name__ == "__main__":
    main()
