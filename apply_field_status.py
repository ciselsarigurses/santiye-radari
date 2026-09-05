"""GitHub Issue başlığından saha görevi veya alarm-dışı kalibrasyon sonucunu uygular."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from calibration_outcome import calibration_id_aliases, save_calibration_outcome
from field_outcome import clear_outcome, save_outcome
from field_state import apply_status
from report_quality import normalize_daily_report


REPORT_FILE = Path(__file__).with_name("latest_report.json")
SHADOW_FILE = Path(__file__).with_name("preseason_dry_ground_shadow.json")
TITLE_PATTERN = re.compile(
    r"^\[SAHA\]\s+([SU][A-Z0-9]+)\s+"
    r"(KONTROLE_GIT|TEKRAR_GIT|KONTROL_EDILDI)"
    r"(?:\s+(SANTIYE_KAZI|YOL_ALTYAPI|TARLA_BITKI|YANLIS_POZITIF))?$"
)
CALIBRATION_TITLE_PATTERN = re.compile(
    r"^\[KALIBRASYON\]\s+(K[A-F0-9]{10})\s+"
    r"(SANTIYE_KAZI|YOL_ALTYAPI|TARLA_BITKI|YANLIS_POZITIF)$"
)


def _report_payload():
    try:
        payload = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _shadow_calibration_items(report_date):
    """Yalnız güvenli, alarm-dışı 250-900 m² ön-sezon gölge adaylarını döndür."""
    try:
        payload = json.loads(SHADOW_FILE.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    if str(payload.get("rapor_tarihi") or "") != str(report_date or ""):
        return []
    if not (
        payload.get("operasyonel") is False
        and payload.get("alarm") is False
        and payload.get("kalici_saha_gorevi") is False
        and payload.get("ana_alarm_alt_esigi_m2") == 250
        and payload.get("diagnostik_aralik_m2") == [250, 900]
    ):
        return []

    selected = []
    for raw in payload.get("adaylar") or []:
        if not isinstance(raw, dict):
            continue
        try:
            area = float(raw.get("alan_m2") or 0)
        except (TypeError, ValueError):
            continue
        if not (
            250 <= area <= 900
            and raw.get("gölge_kalibrasyon") is True
            and raw.get("alarm") is False
            and raw.get("saha_gorevi") is False
        ):
            continue

        # Gölge dosyası güçlü-pair geçidinden çıktığı için bu kanıtlar çıkarımsal değil,
        # guard'ın zorunlu koşullarıdır. Geri bildirim snapshot'ına mevcut ölçüleri taşı.
        item = dict(raw)
        item["kalibrasyon_kaynagi"] = "ON_SEZON_KURU_ZEMIN_GOLGE"
        item["ortalama_bsi_degisim"] = raw.get("son_cift_bsi_degisim")
        item["izole_saha_benzeri"] = True
        item["lineer_geometri_riski"] = False
        item["zaman_serisi_ani_baslangic_destegi"] = True
        item["zaman_serisi_ani_baslangic_orani"] = raw.get(
            "uzun_temporal_ani_baslangic_orani"
        )
        item["zaman_serisi_istikrarsiz_zemin_riski"] = False
        item["yerellik_lokal_ani_baslangic_destegi"] = True
        item["yerellik_yaygin_cevre_degisim_riski"] = False
        selected.append(item)
    return selected


def _calibration_item(calibration_key):
    payload = _report_payload()
    for item in payload.get("kuru_zemin_kalibrasyon_kontrolu", []) or []:
        if not isinstance(item, dict):
            continue
        if calibration_key in calibration_id_aliases(item):
            return item

    # Yasak dönemindeki çok güçlü kuru-zemin gölge adayları normal saha görevine
    # dönüşmez; fakat kullanıcı zaten yakınındaysa verdiği etiket ayrı kalibrasyon
    # tablosuna kaydedilebilsin. Kimlik yine dosyadaki ham aday üzerinden doğrulanır.
    for item in _shadow_calibration_items(payload.get("rapor_tarihi")):
        if calibration_key in calibration_id_aliases(item):
            return item
    return None


def _field_item(task_id):
    """Sonucu işlenen Sentinel görevinin kullanıcıya gösterilen özelliklerini bul."""
    task_id = str(task_id or "").strip().upper()
    if not task_id.startswith("U"):
        return None
    payload = _report_payload()
    for item in payload.get("saha_adaylari", []) or []:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("gorev_id") or "").strip().upper()
        if candidate_id == task_id:
            return dict(item)
    return None


def apply_issue_title(title):
    normalized = str(title or "").strip().upper()

    calibration_match = CALIBRATION_TITLE_PATTERN.fullmatch(normalized)
    if calibration_match:
        calibration_key, outcome = calibration_match.groups()
        item = _calibration_item(calibration_key)
        if item is None:
            raise ValueError(
                "Kalibrasyon noktası güncel raporda veya aynı-gün güçlü gölge havuzunda bulunamadı; "
                "eski ya da doğrulanmamış kimlik kaydedilmedi."
            )
        save_calibration_outcome(calibration_key, outcome, item)
        return {
            "gorev_id": calibration_key,
            "eski_durum": "ALARM_DEGIL",
            "yeni_durum": "KALIBRASYON_KAYDEDILDI",
            "kaynak": "kalibrasyon",
            "sonuc": outcome,
        }, None

    match = TITLE_PATTERN.fullmatch(normalized)
    if not match:
        raise ValueError(
            "Geçersiz talep. Beklenen: [SAHA] <görev> <durum> [sonuç] veya "
            "[KALIBRASYON] <kimlik> <sonuç>"
        )
    task_id, status, outcome = match.groups()
    if outcome and status != "KONTROL_EDILDI":
        raise ValueError("Saha sonucu yalnızca KONTROL_EDILDI durumuyla kaydedilebilir.")

    # KONTROL_EDILDI işlemi günlük raporu yeniden normalize edip görevi listeden
    # çıkarabilir. Bu nedenle Sentinel özellik snapshot'ını durum değişmeden önce al.
    field_item = _field_item(task_id) if outcome else None
    result = apply_status(task_id, status)
    if outcome:
        result["sonuc"] = save_outcome(task_id, outcome, field_item)
    elif status != "KONTROL_EDILDI":
        clear_outcome(task_id)

    report = normalize_daily_report()
    return result, report


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        raise SystemExit("Kullanım: python apply_field_status.py '<issue başlığı>'")
    result, report = apply_issue_title(argv[0])
    if result.get("kaynak") == "kalibrasyon":
        message = (
            f"{result['gorev_id']}: alarm-dışı kalibrasyon sonucu kaydedildi · "
            f"sonuç={result['sonuc']}"
        )
    else:
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
