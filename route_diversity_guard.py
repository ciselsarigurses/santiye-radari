"""Alarm dışı saha kontrol katmanlarının aynı mahalleyi gereksiz tekrar etmesini azaltır.

Günlük rota kısa listesi kuru-zemin diagnostiklerinden en fazla iki kalibrasyon noktası,
Sentinel kör alan denetimi de en fazla iki kapsama noktası gösterebilir. Her iki katman
ayrı ayrı güvenli olsa da aynı gün aynı yaklaşık mahalleleri seçmeleri yarımada kapsamasını
daraltır. Bu post-process katmanı alarm/görev üretmez, toplam nokta sayısını artırmaz ve
mevcut kör-alan seçimindeki mesafe/alan güvenliklerini değiştirmez. Yalnız güvenli bir
alternatif varsa kör-alan havuzunda, kuru-zemin kalibrasyonunda zaten temsil edilen
mahalleleri ikinci kez seçmemeyi tercih eder.

Çeşme ve Uzunkuyu Sentinel kutuları bilinçli olarak örtüşür. Bu örtüşme yüzünden doğu
kutusu bazen Alaçatı/Şifne gibi batı kutusunun da gördüğü bir noktayı seçip gerçek doğu
çekirdeğini o gün saha devriyesiz bırakabiliyordu. Bu nedenle gerçek ``cesme`` ve
``uzunkuyu`` bölgelerinde önce diğer kutunun kapsamadığı çekirdek bölüm denenir; güvenli
çekirdek nokta bulunamazsa mevcut örtüşme havuzuna geri düşülür. Çekirdek tercihi alarm,
görev veya eşik üretmez; yalnız aynı iki saha kontrolünün coğrafi kapsamasını genişletir.

Örtüşen iki Sentinel bölgesinden birinde alternatif mahalle sayısı azsa o bölge önce
seçilir. Böylece örneğin doğu havuzunda kalibrasyon dışı tek mahalle Alaçatı iken batı
havuzunda Ovacık/Alaçatı gibi birkaç seçenek varsa, batı önce Alaçatı'yı tüketip doğuyu
aynı mahalleye zorlamaz. Bu yalnız sıralama tercihidir; mevcut mesafe ve güvenlik
filtreleri aynen uygulanır.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import coverage_patrol_shortlist as coverage
import satellite


AUDIT_JSON = Path(__file__).with_name("coverage_blind_area_audit.json")
REPORT_JSON = Path(__file__).with_name("latest_report.json")
FIELD_REPORT_MD = Path(__file__).with_name("SAHA_RAPORU.md")
NOTE = (
    "Alarm/görev değildir; tarihsel Sentinel verisinde kara olduğu doğrulanmış kalıcı "
    "gözlem boşluklarından günlük rotasyonla en fazla iki insan kontrol noktası seçilir. "
    "Aktif radar görevlerinden >=150 m, birbirinden >=250 m uzakta tutulur. Çeşme ve "
    "Uzunkuyu kutularında güvenli aday varsa önce diğer kutunun kapsamadığı bölge çekirdeği "
    "tercih edilir; çekirdek bulunamazsa örtüşme alanına geri düşülür. Kuru zemin "
    "kalibrasyonunda aynı gün zaten temsil edilen mahalleler, güvenli alternatif varsa "
    "kör alan devriyesinde tekrar seçilmez; mahalle seçeneği daha kısıtlı uydu bölgesi "
    "önce değerlendirilir."
)


def _neighborhoods_from_calibration(report_payload):
    values = set()
    for item in (report_payload or {}).get("kuru_zemin_kalibrasyon_kontrolu") or []:
        if not isinstance(item, dict):
            continue
        key = coverage._neighborhood_key(item.get("mahalle"))
        if key:
            values.add(key)
    return values


def _prefer_other_neighborhoods(audit_payload, blocked_neighborhoods):
    """Her bölgede güvenli alternatif varsa kalibrasyon mahallesini havuzdan çıkar."""
    adjusted = copy.deepcopy(audit_payload)
    if not blocked_neighborhoods:
        return adjusted

    regions = adjusted.get("bolgeler") or {}
    if not isinstance(regions, dict):
        return adjusted

    for region_data in regions.values():
        if not isinstance(region_data, dict):
            continue
        for field in ("kor_alan_devriye_ornekleri", "ornekler"):
            examples = region_data.get(field)
            if not isinstance(examples, list) or not examples:
                continue
            alternatives = [
                item
                for item in examples
                if isinstance(item, dict)
                and coverage._neighborhood_key(item.get("mahalle_yaklasik"))
                not in blocked_neighborhoods
            ]
            # Tercih yalnız alternatif gerçekten varsa uygulanır; aksi halde bölgeyi
            # kör bırakmamak için orijinal havuz korunur.
            if alternatives:
                region_data[field] = alternatives
    return adjusted


def _is_region_core_example(region_key, item):
    """Örtüşen iki üretim kutusunda yalnız ilgili kutunun gördüğü boylam çekirdeğini ölç."""
    if not isinstance(item, dict):
        return False
    try:
        longitude = float(item.get("boylam"))
    except (TypeError, ValueError):
        return False

    if region_key == "cesme":
        # Uzunkuyu kutusu 26.45 E'de başlar. Bunun batısı yalnız Çeşme kutusundadır.
        return longitude < float(satellite.REGIONS["uzunkuyu"]["bbox"][0])
    if region_key == "uzunkuyu":
        # Çeşme kutusu 26.53 E'de biter. Bunun doğusu yalnız Uzunkuyu kutusundadır.
        return longitude > float(satellite.REGIONS["cesme"]["bbox"][2])
    # Sentetik/gelecekteki bölge anahtarlarında eski davranışı koru.
    return True


def _prefer_region_core(audit_payload):
    """Gerçek iki bölgede çekirdek örnek varsa önce yalnız çekirdeği dene."""
    adjusted = copy.deepcopy(audit_payload)
    regions = adjusted.get("bolgeler") or {}
    if not isinstance(regions, dict):
        return adjusted

    for region_key, region_data in regions.items():
        if region_key not in {"cesme", "uzunkuyu"} or not isinstance(region_data, dict):
            continue
        for field in ("kor_alan_devriye_ornekleri", "ornekler"):
            examples = region_data.get(field)
            if not isinstance(examples, list) or not examples:
                continue
            core = [item for item in examples if _is_region_core_example(region_key, item)]
            if core:
                region_data[field] = core
    return adjusted


def _candidate_neighborhood_count(region_key, region_data):
    """Mevcut güvenli aday havuzundaki yaklaşık mahalle çeşitliliğini say."""
    pool = coverage._candidate_pool(region_key, region_data)
    values = {
        coverage._neighborhood_key(item.get("mahalle"))
        for item in pool
        if coverage._neighborhood_key(item.get("mahalle"))
    }
    return len(values)


def _constrained_regions_first(audit_payload):
    """Alternatifi az bölgeyi önce seçerek diğer bölgenin esnekliğini koru."""
    adjusted = copy.deepcopy(audit_payload)
    regions = adjusted.get("bolgeler") or {}
    if not isinstance(regions, dict) or len(regions) <= 1:
        return adjusted

    ordered = sorted(
        regions.items(),
        key=lambda pair: (
            _candidate_neighborhood_count(pair[0], pair[1]) or 10_000,
            str(pair[0]),
        ),
    )
    adjusted["bolgeler"] = dict(ordered)
    return adjusted


def _merge_core_with_fallback(core_selected, fallback_selected, limit):
    """Çekirdek seçimini koru; eksik bölge varsa güvenli normal seçimden tamamla."""
    cap = max(int(limit), 0)
    selected = [dict(item) for item in core_selected[:cap]]
    selected_regions = {str(item.get("bolge_anahtari") or "") for item in selected}
    selected_points = [coverage._point(item) for item in selected if coverage._point(item) is not None]

    for item in fallback_selected:
        if len(selected) >= cap:
            break
        region_key = str(item.get("bolge_anahtari") or "")
        if region_key in selected_regions:
            continue
        point = coverage._point(item)
        if point is None:
            continue
        if not coverage._far_enough(point, selected_points, coverage.CROSS_REGION_DISTANCE_M):
            continue
        selected.append(dict(item))
        selected_regions.add(region_key)
        selected_points.append(point)
    return selected[:cap]


def select_diverse_coverage_patrol(audit_payload, report_payload, limit=coverage.TOTAL_LIMIT):
    blocked = _neighborhoods_from_calibration(report_payload)

    # Önce gerçek coğrafi çekirdeği dene. Mahalle çeşitliliği çekirdeğin içinde yine
    # uygulanır; çekirdekte tek güvenli mahalle varsa sırf adı kalibrasyonla aynı diye
    # doğu/batı çekirdeği tamamen terk edilmez.
    core_adjusted = _prefer_region_core(audit_payload)
    core_adjusted = _prefer_other_neighborhoods(core_adjusted, blocked)
    core_adjusted = _constrained_regions_first(core_adjusted)
    core_selected = coverage.select_coverage_patrol(
        core_adjusted,
        report_payload,
        limit=limit,
    )

    # Eski davranış güvenli geri dönüş olarak korunur. Bir çekirdekte aktif görev veya
    # mesafe engeli yüzünden nokta kalmazsa yalnız eksik bölge örtüşme havuzundan tamamlanır.
    adjusted = _prefer_other_neighborhoods(audit_payload, blocked)
    adjusted = _constrained_regions_first(adjusted)
    fallback_selected = coverage.select_coverage_patrol(
        adjusted,
        report_payload,
        limit=limit,
    )
    return _merge_core_with_fallback(core_selected, fallback_selected, limit)


def _markdown(items):
    section = coverage._markdown(items)
    old = "Aynı görüntü günlerce değişmezse noktalar günlük rotasyonla değişir."
    new = (
        old
        + " Çeşme ve Uzunkuyu kutularında güvenli aday varsa önce diğer kutunun "
        "kapsamadığı bölge çekirdeği tercih edilir. Kuru zemin kalibrasyonunda aynı "
        "gün zaten temsil edilen mahalleler, güvenli alternatif varsa burada tekrar "
        "seçilmez; mahalle alternatifi daha kısıtlı uydu bölgesi önce değerlendirilir."
    )
    return section.replace(old, new, 1)


def update_route_diversity():
    if not AUDIT_JSON.exists() or not REPORT_JSON.exists():
        return []

    audit = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
    report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    selected = select_diverse_coverage_patrol(audit, report)

    report["kor_alan_saha_devriyesi"] = selected
    report["kor_alan_saha_devriyesi_notu"] = NOTE
    REPORT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if FIELD_REPORT_MD.exists():
        current = FIELD_REPORT_MD.read_text(encoding="utf-8")
        FIELD_REPORT_MD.write_text(
            coverage._inject(current, _markdown(selected)),
            encoding="utf-8",
        )
    return selected


def _self_check():
    coverage._self_check()
    audit = {
        "bolgeler": {
            "west": {
                "durum": "ok",
                "bolge": "Batı",
                "kara_referans_sahne_sayisi": 8,
                "kor_alan_devriye_ornekleri": [
                    {
                        "mahalle_yaklasik": "Dalyan",
                        "enlem": 38.35,
                        "boylam": 26.30,
                        "alan_m2": 400,
                        "neden": "BULUT_GOLGE_KALICI",
                    },
                    {
                        "mahalle_yaklasik": "Ovacık",
                        "enlem": 38.25,
                        "boylam": 26.33,
                        "alan_m2": 500,
                        "neden": "BULUT_GOLGE_KALICI",
                    },
                ],
            },
            "east": {
                "durum": "ok",
                "bolge": "Doğu",
                "kara_referans_sahne_sayisi": 8,
                "kor_alan_devriye_ornekleri": [
                    {
                        "mahalle_yaklasik": "Ildır",
                        "enlem": 38.40,
                        "boylam": 26.48,
                        "alan_m2": 400,
                        "neden": "BULUT_GOLGE_KALICI",
                    },
                    {
                        "mahalle_yaklasik": "Germiyan",
                        "enlem": 38.32,
                        "boylam": 26.50,
                        "alan_m2": 600,
                        "neden": "KARISIK_GECERSIZLIK",
                    },
                ],
            },
        }
    }
    report = {
        "rapor_tarihi": "2026-08-31",
        "saha_adaylari": [],
        "kuru_zemin_kalibrasyon_kontrolu": [
            {"mahalle": "Dalyan", "enlem": 38.36, "boylam": 26.31},
            {"mahalle": "Ildır", "enlem": 38.40, "boylam": 26.62},
        ],
    }
    selected = select_diverse_coverage_patrol(audit, report)
    assert {item["mahalle"] for item in selected} == {"Ovacık", "Germiyan"}, selected
    assert all(item["alarm"] is False for item in selected)
    assert len(selected) == coverage.TOTAL_LIMIT

    # Gerçek veride görülen kenar durum: doğu bölgesinde kalibrasyon dışı tek
    # mahalle Alaçatı, batıda ise Alaçatı yanında Ovacık da var. Kısıtlı doğu önce
    # seçilmeli ki batı güvenli Ovacık alternatifine kayabilsin.
    scarcity_audit = {
        "bolgeler": {
            "west": {
                "durum": "ok",
                "bolge": "Batı",
                "kara_referans_sahne_sayisi": 8,
                "kor_alan_devriye_ornekleri": [
                    {"mahalle_yaklasik": "Alaçatı", "enlem": 38.20, "boylam": 26.40, "alan_m2": 400, "neden": "BULUT_GOLGE_KALICI"},
                    {"mahalle_yaklasik": "Ovacık", "enlem": 38.25, "boylam": 26.33, "alan_m2": 400, "neden": "BULUT_GOLGE_KALICI"},
                ],
            },
            "east": {
                "durum": "ok",
                "bolge": "Doğu",
                "kara_referans_sahne_sayisi": 8,
                "kor_alan_devriye_ornekleri": [
                    {"mahalle_yaklasik": "Alaçatı", "enlem": 38.19, "boylam": 26.46, "alan_m2": 400, "neden": "BULUT_GOLGE_KALICI"},
                ],
            },
        }
    }
    scarcity = select_diverse_coverage_patrol(
        scarcity_audit,
        {"rapor_tarihi": "2026-08-31", "saha_adaylari": [], "kuru_zemin_kalibrasyon_kontrolu": []},
    )
    assert [item["bolge_anahtari"] for item in scarcity] == ["east", "west"], scarcity
    assert {item["mahalle"] for item in scarcity} == {"Alaçatı", "Ovacık"}, scarcity

    # Gerçek Çeşme/Uzunkuyu kutu adlarıyla çekirdek koruması: kalibrasyon mahallesi
    # aynı olsa bile doğu kutusu batıdaki örtüşme noktasına kaçmamalı; 26.53 E doğusundaki
    # güvenli çekirdek noktası seçilmelidir.
    core_audit = {
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "bolge": "Çeşme",
                "kara_referans_sahne_sayisi": 8,
                "kor_alan_devriye_ornekleri": [
                    {"mahalle_yaklasik": "Alaçatı", "enlem": 38.20, "boylam": 26.50, "alan_m2": 400, "neden": "BULUT_GOLGE_KALICI"},
                    {"mahalle_yaklasik": "Dalyan", "enlem": 38.36, "boylam": 26.31, "alan_m2": 400, "neden": "BULUT_GOLGE_KALICI"},
                ],
            },
            "uzunkuyu": {
                "durum": "ok",
                "bolge": "Uzunkuyu",
                "kara_referans_sahne_sayisi": 8,
                "kor_alan_devriye_ornekleri": [
                    {"mahalle_yaklasik": "Alaçatı", "enlem": 38.19, "boylam": 26.46, "alan_m2": 400, "neden": "BULUT_GOLGE_KALICI"},
                    {"mahalle_yaklasik": "Uzunkuyu", "enlem": 38.19, "boylam": 26.62, "alan_m2": 400, "neden": "BULUT_GOLGE_KALICI"},
                ],
            },
        }
    }
    core_report = {
        "rapor_tarihi": "2026-09-01",
        "saha_adaylari": [],
        "kuru_zemin_kalibrasyon_kontrolu": [
            {"mahalle": "Uzunkuyu", "enlem": 38.33, "boylam": 26.64},
        ],
    }
    core_selected = select_diverse_coverage_patrol(core_audit, core_report)
    assert len(core_selected) == 2, core_selected
    core_by_region = {item["bolge_anahtari"]: item for item in core_selected}
    assert float(core_by_region["cesme"]["boylam"]) < satellite.REGIONS["uzunkuyu"]["bbox"][0]
    assert float(core_by_region["uzunkuyu"]["boylam"]) > satellite.REGIONS["cesme"]["bbox"][2]

    fallback_audit = {
        "bolgeler": {
            "only": {
                "durum": "ok",
                "bolge": "Tek",
                "kara_referans_sahne_sayisi": 8,
                "kor_alan_devriye_ornekleri": [
                    {
                        "mahalle_yaklasik": "Dalyan",
                        "enlem": 38.35,
                        "boylam": 26.30,
                        "alan_m2": 400,
                        "neden": "BULUT_GOLGE_KALICI",
                    }
                ],
            }
        }
    }
    fallback = select_diverse_coverage_patrol(fallback_audit, report, limit=1)
    assert fallback and fallback[0]["mahalle"] == "Dalyan"


if __name__ == "__main__":
    _self_check()
    chosen = update_route_diversity()
    print(
        "Saha devriye çeşitliliği güncellendi: "
        + (", ".join(str(item.get("mahalle") or "?") for item in chosen) or "ek nokta yok")
    )
