"""Doğrulanmış yıkım/parsel temizliği sonrası yeni Sentinel hareketini öne alır.

Bu guard yalnız 15 Eylül 2026 ve sonrasında çalışır. Yeni alarm veya saha görevi
üretmez; 250 m² ana Sentinel alt eşiğini ve 150–249 m² MİKRO ŞANTİYE diagnostik
politikasını değiştirmez. Ama sahada YIKIM_TEMIZLIK olarak doğrulanmış bir noktanın
25 m yakınında, daha sonraki Sentinel sahnesinde bağımsız 250 m²+ yeni ve eyleme
dönük bir görev oluşursa, bu görevi ERKEN/PARSEL etiketi taşımasa bile günlük rota
sıralamasında taze kazı/temel sinyali kadar güçlü bir operasyon kanıtı sayar.

Amaç, sahada doğrulanan "yıkım genellikle yeni inşaatın başlangıcıdır" bilgisini
sıralamada kullanırken geniş homojen yüzey, kıyı/su, eski taşınmış kanıt veya MİKRO
katmanı yanlışlıkla alarma yükseltmemektir.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path

import daily_route_freshness_guard as freshness
import daily_route_shortlist as route
import postseason_excavation_priority_guard as base


BASE = Path(__file__).resolve().parent
REPORT_JSON = BASE / "latest_report.json"
FIELD_REPORT_MD = BASE / "SAHA_RAPORU.md"
MAIN_ALARM_MIN_M2 = 250
FOLLOWUP_MAX_M2 = 5_000
MATCH_DISTANCE_M = 25.0
MAX_AGE_DAYS = 60
FULL_OPERATION_START = date(2026, 9, 15)
NOTE = (
    base.NOTE
    + " Sahada doğrulanmış yıkım/parsel temizliğiyle 25 m içinde çakışan, sonraki "
    "Sentinel sahnesine ait mevcut 250 m²+ eyleme dönük görev ERKEN/PARSEL etiketi "
    "taşımasa da yeni inşaat devam sinyali olarak üst sıraya alınır. Bu kural yeni "
    "alarm/görev üretmez ve MİKRO 150–249 m² bandını yükseltmez."
)


def _eligible_new_main_candidate(item):
    """Yıkım teyidiyle güçlenebilecek mevcut taze ana-görevin güvenlik kapısı."""
    if not isinstance(item, dict) or base._is_manual_repeat(item):
        return False

    area = max(base._number(item.get("alan_m2"), 0.0), 0.0)
    if not (MAIN_ALARM_MIN_M2 <= area <= FOLLOWUP_MAX_M2):
        return False
    if item.get("yeni_goruntu") is not True:
        return False

    evidence_day = base._parse_scene_date(item.get("son_tarih"))
    if evidence_day is None or evidence_day < FULL_OPERATION_START:
        return False
    if freshness._historical_evidence_rank(item) != 0:
        return False

    if item.get("genis_geometri_riski") is True:
        return False
    if str(item.get("izleme") or "").strip().upper() == "ARKA_PLAN_GENIS_YUZEY":
        return False
    if item.get("alarm") is False or item.get("saha_gorevi") is False:
        return False
    return True


def _demolition_match(item, field_precursors):
    """Taze mevcut görev ile daha eski doğrulanmış yıkımı güvenli biçimde eşleştir."""
    if not _eligible_new_main_candidate(item):
        return None
    if not isinstance(field_precursors, (list, tuple)):
        return None

    current_date = base._parse_scene_date(item.get("son_tarih"))
    latitude = base._number(item.get("enlem"), None)
    longitude = base._number(item.get("boylam"), None)
    if current_date is None or latitude is None or longitude is None:
        return None

    best = None
    best_distance = None
    for entry in field_precursors:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sonuc") or "").strip().upper() != "YIKIM_TEMIZLIK":
            continue

        previous_date = base._parse_scene_date(entry.get("son_tarih"))
        if previous_date is None or previous_date >= current_date:
            continue
        age_days = (current_date - previous_date).days
        if age_days > MAX_AGE_DAYS:
            continue

        previous_lat = base._number(entry.get("enlem"), None)
        previous_lon = base._number(entry.get("boylam"), None)
        previous_area = base._number(entry.get("alan_m2"), None)
        if previous_lat is None or previous_lon is None or previous_area is None:
            continue
        if not (base.FIELD_PRECURSOR_MIN_M2 <= previous_area <= base.FIELD_PRECURSOR_MAX_M2):
            continue

        distance = base._distance_m(latitude, longitude, previous_lat, previous_lon)
        if distance > MATCH_DISTANCE_M:
            continue
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best = {
                "yikim_takip_onceligi": True,
                "saha_oncul_eslesmesi": True,
                "saha_oncul_sonuc": "YIKIM_TEMIZLIK",
                "saha_oncul_gorev_id": str(entry.get("gorev_id") or ""),
                "saha_oncul_mesafe_m": round(distance, 1),
                "saha_oncul_son_tarih": previous_date.strftime("%d.%m.%Y"),
                "saha_oncul_kanit_notu": (
                    "Sahada doğrulanmış yıkım/parsel temizliğiyle sonraki yeni Sentinel "
                    "sahnesindeki mevcut 250 m²+ görev 25 m içinde örtüşüyor. Yıkım "
                    "yüksek olasılıklı yeni inşaat başlangıcı olarak operasyon sırasını "
                    "güçlendirir; tek başına yeni alarm veya görev üretmez."
                ),
            }
    return best


def _route_key(item, original_index, field_precursors):
    if base._is_manual_repeat(item):
        band = 0
        distance_rank = 0.0
    else:
        match = _demolition_match(item, field_precursors)
        if match:
            band = 1
            distance_rank = float(match.get("saha_oncul_mesafe_m") or MATCH_DISTANCE_M)
        elif base._is_fresh_excavation_candidate(item):
            band = 2
            distance_rank = MATCH_DISTANCE_M + 1
        else:
            band = 3
            distance_rank = MATCH_DISTANCE_M + 1
    return (band, distance_rank, *route._route_sort_key(item, original_index))


def select_shortlist(candidates, field_precursors, limit=route.SHORTLIST_LIMIT):
    """Mevcut eyleme dönük görevleri yıkım teyidi kanıtıyla yeniden sırala."""
    cap = max(int(limit), 0)
    if cap <= 0:
        return []

    eligible = freshness._normalized_actionable_candidates(candidates)
    indexed = list(enumerate(eligible))
    indexed.sort(key=lambda pair: _route_key(pair[1], pair[0], field_precursors))

    selected = []
    seen = set()
    for _, raw in indexed:
        task_id = str(raw.get("gorev_id") or "").strip()
        if task_id and task_id in seen:
            continue
        item = dict(raw)
        match = _demolition_match(item, field_precursors)
        if match:
            item.update(match)
        if task_id:
            seen.add(task_id)
        selected.append(item)
        if len(selected) >= cap:
            break

    for index, item in enumerate(selected, start=1):
        item["gunluk_sira"] = index
    return selected


def _shortlist_markdown(shortlist):
    lines = [route.SECTION_TITLE, "", f"> {NOTE}", ""]
    if not shortlist:
        lines.extend(["Bugün için eyleme dönük aktif uydu görevi yok.", ""])
        return "\n".join(lines)

    for item in shortlist:
        order = int(item.get("gunluk_sira") or 0)
        priority = str(item.get("oncelik") or "KONTROL")
        neighborhood = str(item.get("mahalle") or "Konum araştırılıyor")
        area = max(base._number(item.get("alan_m2"), 0), 0)
        area_text = f" · yaklaşık {int(area):,} m²".replace(",", ".") if area else ""
        task_id = str(item.get("gorev_id") or "-")
        map_url = str(item.get("harita") or "").strip()
        route_text = f" · [Yol tarifi]({map_url})" if map_url.startswith(("http://", "https://")) else ""
        demolition_tag = " · **YIKIM→YENİ HAREKET**" if item.get("yikim_takip_onceligi") else ""
        fresh_tag = " · **TAZE KAZI ÖNCELİĞİ**" if base._is_fresh_excavation_candidate(item) else ""
        micro_tag = " · **MİKRO→ANA DEVAM KANITI**" if item.get("mikro_oncul_iz_eslesmesi") else ""
        lines.append(
            f"{order}. **{priority} — {neighborhood}**{area_text}{demolition_tag}{fresh_tag}{micro_tag} · "
            f"Görev `{task_id}`{route_text}"
        )
    lines.append("")
    return "\n".join(lines)


def apply_demolition_followup_priority(local_day=None):
    day = base._local_day(local_day)
    if day < FULL_OPERATION_START:
        return False, []

    payload = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    candidates = payload.get("saha_adaylari") or []
    field_precursors = base._load_field_precursors()
    shortlist = select_shortlist(candidates, field_precursors)
    matched_ids = [
        str(item.get("gorev_id") or "")
        for item in shortlist
        if item.get("yikim_takip_onceligi") is True
    ]

    payload["gunun_ilk_3_kontrolu"] = shortlist
    payload["gunun_ilk_3_notu"] = NOTE
    payload["demolition_followup_priority"] = {
        "aktif": True,
        "baslangic_tarihi": FULL_OPERATION_START.isoformat(),
        "ana_alarm_alt_esigi_m2": MAIN_ALARM_MIN_M2,
        "mikro_santiye": "150-249 m² diagnostik; doğrudan saha görevine yükseltilmez",
        "esleme_mesafesi_m": MATCH_DISTANCE_M,
        "azami_yas_gun": MAX_AGE_DAYS,
        "eslesen_gorevler": matched_ids,
        "not": (
            "Yalnız mevcut eyleme dönük görevleri yeniden sıralar. Sahada doğrulanmış "
            "YIKIM_TEMIZLIK sonucu, sonraki yeni Sentinel sahnesinde aynı noktadaki "
            "250 m²+ görevi güçlü yeni-inşaat devam sinyali yapar; yeni alarm/görev üretmez."
        ),
    }

    before = REPORT_JSON.read_text(encoding="utf-8")
    after = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    changed = before != after
    if changed:
        REPORT_JSON.write_text(after, encoding="utf-8")

    if FIELD_REPORT_MD.exists():
        current = FIELD_REPORT_MD.read_text(encoding="utf-8")
        updated = route._inject_markdown(current, _shortlist_markdown(shortlist))
        if updated != current:
            FIELD_REPORT_MD.write_text(updated, encoding="utf-8")
            changed = True
    return changed, shortlist


def _self_check():
    west = freshness.CANONICAL_WEST_REGION
    east = freshness.CANONICAL_EAST_REGION
    field = [
        {
            "gorev_id": "DEMOLITION_OLD",
            "sonuc": "YIKIM_TEMIZLIK",
            "enlem": 38.33155,
            "boylam": 26.64434,
            "alan_m2": 400,
            "son_tarih": "08.09.2026",
        }
    ]
    demolition_followup = {
        "gorev_id": "FOLLOWUP_NORMAL",
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "NORMAL",
        "mahalle": "Gülbahçe",
        "enlem": 38.33156,
        "boylam": 26.64435,
        "alan_m2": 1200,
        "bolge": east,
        "onceki_tarih": "15.09.2026",
        "son_tarih": "16.09.2026",
        "yeni_goruntu": True,
        "uydu_onceligi": "NORMAL",
        "boyut_sinifi": "STANDART",
        "sinyal": "Yeni yüzey/toprak değişimi",
    }
    ordinary_fresh = {
        "gorev_id": "ORDINARY_FRESH",
        "saha_durumu": "KONTROLE_GIT",
        "oncelik": "ERKEN",
        "mahalle": "Alaçatı",
        "enlem": 38.285,
        "boylam": 26.375,
        "alan_m2": 600,
        "bolge": west,
        "onceki_tarih": "15.09.2026",
        "son_tarih": "16.09.2026",
        "yeni_goruntu": True,
        "uydu_onceligi": "YÜKSEK",
        "boyut_sinifi": "KUCUK",
        "sinyal": "Küçük, güçlü yüzey/toprak değişimi adayı",
    }
    repeat = dict(ordinary_fresh)
    repeat.update({"gorev_id": "REPEAT", "saha_durumu": "TEKRAR_GIT", "oncelik": "TEKRAR"})

    assert _eligible_new_main_candidate(demolition_followup)
    assert not base._is_fresh_excavation_candidate(demolition_followup)
    match = _demolition_match(demolition_followup, field)
    assert match and match["saha_oncul_mesafe_m"] <= MATCH_DISTANCE_M

    micro = dict(demolition_followup)
    micro.update({"gorev_id": "MICRO", "alan_m2": 200})
    assert _demolition_match(micro, field) is None
    broad = dict(demolition_followup)
    broad.update({"gorev_id": "BROAD", "genis_geometri_riski": True})
    assert _demolition_match(broad, field) is None
    old_scene = dict(demolition_followup)
    old_scene.update({"gorev_id": "OLD", "son_tarih": "14.09.2026"})
    assert _demolition_match(old_scene, field) is None
    not_new = dict(demolition_followup)
    not_new.update({"gorev_id": "NOT_NEW", "yeni_goruntu": False})
    assert _demolition_match(not_new, field) is None

    wrong_field = json.loads(json.dumps(field))
    wrong_field[0]["sonuc"] = "TARLA_BITKI"
    assert _demolition_match(demolition_followup, wrong_field) is None
    far_field = json.loads(json.dumps(field))
    far_field[0]["enlem"] = 38.3400
    assert _demolition_match(demolition_followup, far_field) is None

    selected = select_shortlist([ordinary_fresh, demolition_followup], field, limit=2)
    assert [item["gorev_id"] for item in selected] == ["FOLLOWUP_NORMAL", "ORDINARY_FRESH"], selected
    assert selected[0]["yikim_takip_onceligi"] is True

    selected_repeat = select_shortlist([ordinary_fresh, demolition_followup, repeat], field, limit=3)
    assert selected_repeat[0]["gorev_id"] == "REPEAT", selected_repeat
    assert selected_repeat[1]["gorev_id"] == "FOLLOWUP_NORMAL", selected_repeat

    assert MAIN_ALARM_MIN_M2 == 250
    assert base.MICRO_MIN_M2 == 150 and base.MICRO_MAX_M2 == 249
    print("OK: doğrulanmış yıkım sonrası 250 m²+ yeni hareket öncelik koruması geçti.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        _self_check()
        return

    changed, shortlist = apply_demolition_followup_priority()
    if base._local_day() < FULL_OPERATION_START:
        print("Kalibrasyon modu: 15 Eylül öncesi rapora dokunulmadı.")
        return
    count = sum(bool(item.get("yikim_takip_onceligi")) for item in shortlist)
    print(f"Yıkım sonrası yeni hareket önceliği uygulandı: ilk listede {count} doğrulanmış takip eşleşmesi.")
    if not changed:
        print("Rapor zaten güncel.")


if __name__ == "__main__":
    main()
