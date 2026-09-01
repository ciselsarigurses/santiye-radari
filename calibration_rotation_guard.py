"""Kuru-zemin kalibrasyon noktalarını aynı Sentinel sahnesinde güvenli biçimde döndürür.

`daily_route_shortlist.py` üretim maskesinin dışında kalan kuru-zemin diagnostiklerinden
iki alarm-dışı kalibrasyon noktası seçer. Sentinel sahnesi birkaç gün aynı kaldığında
saf güç sıralaması her çalışmada aynı noktaları gösterebilir. Bu katman yeni alarm veya
görev üretmeden, aynı güvenlik filtrelerini koruyarak bölge başına en güçlü ilk birkaç
uygun örneği gün gün döndürür. Aynı mahalleden birden fazla güçlü aday ilk rotasyon
havuzunu dolduruyorsa, güvenli başka mahalleler varken bu tekrarlar havuzun dışına
alınır. Yeni Sentinel sahnesi geldiğinde rotasyon sıfırlanır ve yine en güçlü örnekten
başlanır.

Amaç aynı uydu çifti değişmeden beklerken aynı birkaç sokağı tekrar tekrar kontrol etmek
yerine daha fazla mahalleden saha etiketi toplamaktır. Üretim eşiği, 250 m² alt sınırı,
aktif görev mesafesi, günlük kalibrasyon nokta sayısı ve kuru-zemin geometri filtreleri
değişmez.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import daily_route_shortlist as route


DRY_GROUND_AUDIT = Path(__file__).with_name("dry_ground_gap_audit.json")
REPORT_JSON = Path(__file__).with_name("latest_report.json")
FIELD_REPORT_MD = Path(__file__).with_name("SAHA_RAPORU.md")
ROTATION_POOL_PER_REGION = 4


def _parse_report_date(value):
    try:
        return date.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None


def _parse_scene_date(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _days_since_scene(report_date, scene_date):
    current = _parse_report_date(report_date)
    scene = _parse_scene_date(scene_date)
    if current is None or scene is None:
        return 0
    return max((current - scene).days, 0)


def _eligible_region_items(region_key, region_data, active_candidates):
    if not isinstance(region_data, dict) or region_data.get("durum") != "ok":
        return []

    rows = []
    for raw in region_data.get("saha_benzeri_ornekler") or []:
        if not isinstance(raw, dict):
            continue
        area = route._number(raw.get("alan_m2"), 0)
        if not (250 <= area <= 2000):
            continue
        if not bool(raw.get("saha_benzeri_geometri")):
            continue
        if not bool(raw.get("izole_saha_benzeri")):
            continue
        if bool(raw.get("lineer_geometri_riski")):
            continue
        if not route._far_from_active(raw, active_candidates):
            continue

        point = route._point(raw)
        if point is None:
            continue
        item = dict(raw)
        item["bolge_anahtari"] = str(region_key)
        item["bolge"] = str(region_data.get("bolge") or region_key)
        item["onceki_tarih"] = str(region_data.get("onceki_tarih") or "")
        item["son_tarih"] = str(region_data.get("son_tarih") or "")
        item["enlem"] = round(point[0], 6)
        item["boylam"] = round(point[1], 6)
        item["harita"] = (
            "https://www.google.com/maps/dir/?api=1&destination="
            f"{item['enlem']:.6f},{item['boylam']:.6f}"
        )
        item["kalibrasyon_durumu"] = "ALARM_DEGIL"
        rows.append(item)

    return sorted(
        rows,
        key=lambda item: (
            -abs(route._number(item.get("ortalama_bsi_degisim"), 0)),
            -route._number(item.get("ortalama_rgb_farki"), 0),
            -route._number(item.get("kompaktlik"), 0),
            route._number(item.get("alan_m2"), 0),
            str(item.get("mahalle") or ""),
        ),
    )


def _neighborhood_key(value):
    return " ".join(str(value or "").casefold().split())


def _diverse_top_pool(items, pool_size=ROTATION_POOL_PER_REGION):
    """Güç sırasını koruyarak havuzda önce farklı mahalleleri temsil et."""
    cap = max(int(pool_size), 1)
    selected = []
    seen_neighborhoods = set()

    # İlk geçişte her yaklaşık mahalleden en güçlü adayı al. Böylece aynı mahalledeki
    # ikinci güçlü sinyal, başka güvenli mahalleleri dört günlük kalibrasyon döngüsünden
    # çıkarmasın.
    for item in items:
        key = _neighborhood_key(item.get("mahalle")) if isinstance(item, dict) else ""
        if key and key in seen_neighborhoods:
            continue
        selected.append(item)
        if key:
            seen_neighborhoods.add(key)
        if len(selected) >= cap:
            return selected

    # Mahalle sayısı havuzu doldurmaya yetmiyorsa eski davranışa güvenli biçimde dön:
    # kalan en güçlü adayları ekle; hiçbir kalibrasyon slotunu sırf çeşitlilik için silme.
    for item in items:
        if item in selected:
            continue
        selected.append(item)
        if len(selected) >= cap:
            break
    return selected


def _rotated_order(items, offset, pool_size=ROTATION_POOL_PER_REGION):
    if not items:
        return []
    top = _diverse_top_pool(items, pool_size=pool_size)
    rest = [item for item in items if item not in top]
    if len(top) <= 1:
        return top + rest
    shift = max(int(offset), 0) % len(top)
    return top[shift:] + top[:shift] + rest


def select_rotating_calibration(audit_payload, report_payload, limit=route.CALIBRATION_LIMIT):
    """Aynı sahnede güçlü güvenli kalibrasyonları döndür; yeni sahnede en güçlüye dön."""
    cap = max(int(limit), 0)
    if cap <= 0 or not isinstance(audit_payload, dict) or not isinstance(report_payload, dict):
        return []

    regions = audit_payload.get("bolgeler") or {}
    if not isinstance(regions, dict):
        return []

    active = route._actionable_candidates(report_payload.get("saha_adaylari") or [])
    report_date = report_payload.get("rapor_tarihi") or audit_payload.get("rapor_tarihi")
    selected = []

    # Bölge başına bir nokta sınırı korunur. Mevcut iki Sentinel bölgesinde bu,
    # toplam iki kalibrasyon noktasını coğrafi olarak dengede tutar.
    for region_key, region_data in regions.items():
        ranked = _eligible_region_items(region_key, region_data, active)
        if not ranked:
            continue
        age_days = _days_since_scene(report_date, region_data.get("son_tarih"))
        ordered = _rotated_order(ranked, age_days)
        picked = next(
            (item for item in ordered if route._far_from_selected(item, selected)),
            None,
        )
        if picked is None:
            continue
        picked = dict(picked)
        picked["kalibrasyon_rotasyon_gun"] = int(age_days)
        picked["kalibrasyon_rotasyon_havuzu"] = min(
            len(ranked), ROTATION_POOL_PER_REGION
        )
        selected.append(picked)
        if len(selected) >= cap:
            return selected

    return selected[:cap]


def _rotation_markdown(items):
    section = route._calibration_markdown(items)
    old = (
        "Toplam en fazla iki nokta gösterilir; amaç sahada bakıp gerçek hafriyat mı "
        "yanlış pozitif mi olduğunu öğrenerek algoritmayı iki bölgede de kalibre etmektir."
    )
    new = (
        "Toplam en fazla iki nokta gösterilir. Yeni Sentinel sahnesinde en güçlü örnekten "
        "başlanır; aynı sahne kaldıkça bölge başına en güçlü dört güvenli örnek günlük "
        "rotasyonla değiştirilir. Güvenli farklı mahalleler varken aynı mahalleden ikinci "
        "aday bu dört kişilik havuzu dolduramaz. Amaç daha fazla farklı noktadan gerçek "
        "hafriyat / yanlış pozitif saha etiketi toplayarak algoritmayı iki bölgede de "
        "kalibre etmektir."
    )
    return section.replace(old, new, 1)


def update_calibration_rotation():
    if not DRY_GROUND_AUDIT.exists() or not REPORT_JSON.exists():
        return []

    audit = json.loads(DRY_GROUND_AUDIT.read_text(encoding="utf-8"))
    report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    selected = select_rotating_calibration(audit, report)

    report["kuru_zemin_kalibrasyon_kontrolu"] = selected
    report["kuru_zemin_kalibrasyon_notu"] = (
        "Alarm/görev değildir; üretim maskesinin dışındaki izole, saha-benzeri kuru-zemin "
        "değişimlerinden aktif görevlerin en az 120 m dışında bölge başına en fazla bir, "
        "toplam iki örnek seçilir. Yeni Sentinel sahnesinde en güçlü örnekten başlanır; "
        "sahne değişmezse en güçlü dört güvenli örnek günlük rotasyonla değiştirilir. "
        "Güvenli farklı mahalleler varken aynı mahalleden ikinci aday ilk dört kişilik "
        "kalibrasyon havuzuna alınmaz."
    )
    REPORT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if FIELD_REPORT_MD.exists():
        current = FIELD_REPORT_MD.read_text(encoding="utf-8")
        FIELD_REPORT_MD.write_text(
            route._inject_calibration_markdown(current, _rotation_markdown(selected)),
            encoding="utf-8",
        )
    return selected


def _self_check():
    west = route.SATELLITE_REGION_LABELS[0]
    east = route.SATELLITE_REGION_LABELS[1]

    def item(name, lat, lon, bsi):
        return {
            "mahalle": name,
            "enlem": lat,
            "boylam": lon,
            "alan_m2": 400,
            "ortalama_bsi_degisim": bsi,
            "ortalama_rgb_farki": 0.15,
            "kompaktlik": 0.59,
            "saha_benzeri_geometri": True,
            "izole_saha_benzeri": True,
            "lineer_geometri_riski": False,
        }

    audit = {
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "bolge": west,
                "onceki_tarih": "26.08.2026",
                "son_tarih": "29.08.2026",
                "saha_benzeri_ornekler": [
                    item("W1", 38.20, 26.30, 0.40),
                    item("W2", 38.22, 26.32, 0.35),
                    item("W3", 38.24, 26.34, 0.30),
                    item("W4", 38.26, 26.36, 0.25),
                    item("W5", 38.28, 26.38, 0.20),
                ],
            },
            "uzunkuyu": {
                "durum": "ok",
                "bolge": east,
                "onceki_tarih": "26.08.2026",
                "son_tarih": "29.08.2026",
                "saha_benzeri_ornekler": [
                    item("E1", 38.35, 26.55, 0.38),
                    item("E2", 38.37, 26.57, 0.33),
                ],
            },
        }
    }

    fresh = select_rotating_calibration(
        audit, {"rapor_tarihi": "2026-08-29", "saha_adaylari": []}
    )
    assert [row["mahalle"] for row in fresh] == ["W1", "E1"], fresh

    day_two = select_rotating_calibration(
        audit, {"rapor_tarihi": "2026-08-31", "saha_adaylari": []}
    )
    assert [row["mahalle"] for row in day_two] == ["W3", "E1"], day_two
    assert day_two[0]["kalibrasyon_rotasyon_havuzu"] == 4

    next_cycle = select_rotating_calibration(
        audit, {"rapor_tarihi": "2026-09-01", "saha_adaylari": []}
    )
    assert [row["mahalle"] for row in next_cycle] == ["W4", "E2"], next_cycle

    # Aynı mahallenin iki güçlü adayı ilk dört havuzu işgal etmemeli. Spektral güç
    # sırası korunarak güvenli dört farklı mahalle rotasyona girmeli.
    duplicate_neighborhoods = [
        item("A", 38.20, 26.30, 0.40),
        item("A", 38.21, 26.31, 0.39),
        item("B", 38.22, 26.32, 0.38),
        item("C", 38.23, 26.33, 0.37),
        item("D", 38.24, 26.34, 0.36),
    ]
    diverse_pool = _diverse_top_pool(duplicate_neighborhoods, pool_size=4)
    assert [row["mahalle"] for row in diverse_pool] == ["A", "B", "C", "D"], diverse_pool
    diverse_rotation = _rotated_order(duplicate_neighborhoods, 3, pool_size=4)
    assert [row["mahalle"] for row in diverse_rotation[:4]] == ["D", "A", "B", "C"], diverse_rotation

    reset_audit = json.loads(json.dumps(audit))
    for region in reset_audit["bolgeler"].values():
        region["son_tarih"] = "01.09.2026"
    reset = select_rotating_calibration(
        reset_audit, {"rapor_tarihi": "2026-09-01", "saha_adaylari": []}
    )
    assert [row["mahalle"] for row in reset] == ["W1", "E1"], reset

    blocked = {
        "gorev_id": "ACTIVE",
        "saha_durumu": "KONTROLE_GIT",
        "enlem": 38.20,
        "boylam": 26.30,
    }
    safe = select_rotating_calibration(
        audit,
        {"rapor_tarihi": "2026-08-29", "saha_adaylari": [blocked]},
        limit=1,
    )
    assert safe and safe[0]["mahalle"] == "W2", safe
    assert all(row["kalibrasyon_durumu"] == "ALARM_DEGIL" for row in day_two)


if __name__ == "__main__":
    _self_check()
    chosen = update_calibration_rotation()
    print(
        "Kuru zemin kalibrasyon rotasyonu güncellendi: "
        + (", ".join(str(item.get("mahalle") or "?") for item in chosen) or "ek nokta yok")
    )
