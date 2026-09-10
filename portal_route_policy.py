from __future__ import annotations

import re
from datetime import date, datetime

MAIN_SITE_MIN_M2 = 250
DRY_GROUND_CONFIRMATION_MAX_M2 = 900
FULL_OPERATION_START = date(2026, 9, 15)
ACTIVE_STATUSES = {"KONTROLE_GIT", "TEKRAR_GIT"}
PORTAL_ACTIONS = {
    "DOGRU_ADRES_TAKIP",
    "COP_ADRES_KALDIR",
    "POTANSIYEL_MUSTERI",
}
TASK_ID_PATTERN = re.compile(r"^[SU][A-Z0-9]+$")


def parse_date(value):
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def portal_issue_title(task_id: str, action: str) -> str:
    """Portal kararını mevcut saha işleyicisinin anlayacağı güvenli başlığa çevir.

    Ana portal yalnız üç kullanıcı kararını gösterir. ``Doğru adres - takip et``
    yeni bir saha sınıfı uydurmaz; görevi kalıcı ``TEKRAR_GIT`` durumuna geçirir.
    Ayrıntılı saha/kalibrasyon ekranı daha sonra yıkım, kazı vb. fiziksel sonucu
    ayrıca etiketleyebilir. ``Çöp adres`` ise doğrudan doğrulanmış negatif saha
    geri bildirimi olarak kaydedilir. Potansiyel müşteri kaydı saha kalibrasyonunu
    değiştirmeyen ayrı bir GitHub talebi olarak kalır.
    """
    task = str(task_id or "").strip().upper()
    normalized_action = str(action or "").strip().upper()
    if not TASK_ID_PATTERN.fullmatch(task):
        raise ValueError("Geçersiz saha görev kimliği.")
    if normalized_action not in PORTAL_ACTIONS:
        raise ValueError("Bilinmeyen portal saha işlemi.")

    if normalized_action == "DOGRU_ADRES_TAKIP":
        return f"[SAHA] {task} TEKRAR_GIT"
    if normalized_action == "COP_ADRES_KALDIR":
        return f"[SAHA] {task} KONTROL_EDILDI YANLIS_POZITIF"
    return f"[SAHA-PORTAL] {task} POTANSIYEL_MUSTERI"


def _verified_followup_waiting(item: dict) -> bool:
    """Sahada doğrulanmış takip kaydı yeni Sentinel hareketi yoksa pasif kalır.

    Günlük rota üreticisi bu kaydı zaten kısa listeden çıkarır. Portalda da aynı
    korumayı tekrar uygulamak, eski/stale bir ``gunun_ilk_3_kontrolu`` dosyasının
    sahada bakılmış yıkım/parsel temizliği noktasını yanlışlıkla yeniden göstermesini
    engeller. Yeni hareket geldiğinde ``takip_yeni_hareket=True`` olur ve kayıt normal
    TEKRAR_GIT akışına geri döner.
    """
    return bool(
        isinstance(item, dict)
        and item.get("saha_dogrulandi_takip") is True
        and item.get("takip_yeni_hareket") is not True
    )


def is_curated_portal_actionable(item: dict) -> bool:
    """Return True only for operational route rows safe to expose in the field portal.

    Normal KONTROLE_GIT/TEKRAR_GIT rows remain unchanged, except a verified field
    follow-up that is explicitly waiting for newer Sentinel movement stays hidden
    until that movement exists. The only diagnostic exception is the deliberately-
    created, one-day post-season dry-ground confirmation: 250-900 m², a genuinely
    new Sentinel scene dated 15 Sep 2026 or later, and still explicitly non-alarm/
    non-persistent-task. Generic diagnostics and every 150-249 m² MİKRO record remain
    hidden from the field portal.
    """
    if not isinstance(item, dict):
        return False

    status = str(item.get("saha_durumu") or "KONTROLE_GIT").upper()
    if status in ACTIVE_STATUSES:
        if status == "TEKRAR_GIT" and _verified_followup_waiting(item):
            return False
        return True
    if status != "DIAGNOSTIK_DOGRULAMA":
        return False

    if item.get("postseason_kuru_zemin_dogrulama") is not True:
        return False
    if str(item.get("oncelik") or "").upper() not in {"DOĞRULAMA", "DOGRULAMA"}:
        return False
    if item.get("alarm") is not False or item.get("saha_gorevi") is not False:
        return False
    if item.get("yeni_goruntu") is not True:
        return False

    try:
        area_m2 = float(item.get("alan_m2") or 0)
    except (TypeError, ValueError):
        return False
    if not (MAIN_SITE_MIN_M2 <= area_m2 <= DRY_GROUND_CONFIRMATION_MAX_M2):
        return False

    scene_day = parse_date(item.get("son_tarih"))
    if scene_day is None or scene_day < FULL_OPERATION_START:
        return False

    return True


def _self_check() -> None:
    assert portal_issue_title("UABC123", "DOGRU_ADRES_TAKIP") == (
        "[SAHA] UABC123 TEKRAR_GIT"
    )
    assert portal_issue_title("UABC123", "COP_ADRES_KALDIR") == (
        "[SAHA] UABC123 KONTROL_EDILDI YANLIS_POZITIF"
    )
    assert portal_issue_title("UABC123", "POTANSIYEL_MUSTERI") == (
        "[SAHA-PORTAL] UABC123 POTANSIYEL_MUSTERI"
    )
    try:
        portal_issue_title("RADAR-1", "DOGRU_ADRES_TAKIP")
    except ValueError:
        pass
    else:
        raise AssertionError("Uydurma fallback görev kimliği saha durumuna yazılmamalı")

    assert is_curated_portal_actionable({"saha_durumu": "KONTROLE_GIT"})
    assert is_curated_portal_actionable({"saha_durumu": "TEKRAR_GIT"})

    passive_followup = {
        "saha_durumu": "TEKRAR_GIT",
        "saha_dogrulandi_takip": True,
        "takip_yeni_hareket": False,
    }
    assert not is_curated_portal_actionable(passive_followup), (
        "Sahada doğrulanmış takip noktası yeni Sentinel hareketi yokken portala "
        "günlük görev diye dönmemeli"
    )
    assert is_curated_portal_actionable(
        dict(passive_followup, takip_yeni_hareket=True)
    ), "Yeni hareket görülen doğrulanmış takip noktası yeniden eyleme dönmeli"

    valid = {
        "saha_durumu": "DIAGNOSTIK_DOGRULAMA",
        "postseason_kuru_zemin_dogrulama": True,
        "oncelik": "DOĞRULAMA",
        "alarm": False,
        "saha_gorevi": False,
        "yeni_goruntu": True,
        "alan_m2": 300,
        "son_tarih": "2026-09-15",
    }
    assert is_curated_portal_actionable(valid)

    micro = dict(valid, alan_m2=200)
    assert not is_curated_portal_actionable(micro), "150-249 m² MİKRO portal görevi olamaz"
    assert not is_curated_portal_actionable(dict(valid, alan_m2=901))
    assert not is_curated_portal_actionable(dict(valid, son_tarih="2026-09-14"))
    assert not is_curated_portal_actionable(dict(valid, yeni_goruntu=False))
    assert not is_curated_portal_actionable(dict(valid, alarm=True))
    assert not is_curated_portal_actionable(dict(valid, saha_gorevi=True))
    assert not is_curated_portal_actionable(dict(valid, postseason_kuru_zemin_dogrulama=False))
    assert not is_curated_portal_actionable(dict(valid, saha_durumu="MIKRO_GUCLU_DIAGNOSTIK_KANIT"))


if __name__ == "__main__":
    _self_check()
    print("portal_route_policy: OK")
