from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import streamlit as st

from calibration_outcome import calibration_id, calibration_id_aliases, calibration_outcome_map
from field_feedback_audit import feedback_audit_summary
from field_outcome import OUTCOME_LABELS


st.set_page_config(page_title="Kalibrasyon", page_icon="🧪", layout="wide")

ROOT = Path(__file__).resolve().parents[1]
REPORT_FILE = ROOT / "latest_report.json"
SHADOW_FILE = ROOT / "preseason_dry_ground_shadow.json"
ISSUE_URL = "https://github.com/ciselsarigurses/santiye-radari/issues/new"
OUTCOME_BUTTONS = {
    "SANTIYE_KAZI": "🏗️ Şantiye / kazı / temel",
    "YOL_ALTYAPI": "🚧 Yol / altyapı",
    "TARLA_BITKI": "🌿 Tarla / bitki",
    "YANLIS_POZITIF": "❌ Başka neden / yanlış pozitif",
}


def read_report():
    try:
        payload = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def read_shadow_items(report_date):
    """Yalnız aynı-gün, güçlü ve tamamen alarm-dışı ön-sezon gölge adaylarını göster."""
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
        item = dict(raw)
        item["kalibrasyon_kaynagi"] = "ON_SEZON_KURU_ZEMIN_GOLGE"
        item["ortalama_bsi_degisim"] = raw.get("son_cift_bsi_degisim")
        item["zaman_serisi_ani_baslangic_orani"] = raw.get(
            "uzun_temporal_ani_baslangic_orani"
        )
        selected.append(item)
    return selected


def issue_link(item, outcome):
    key = calibration_id(item)
    neighborhood = str(item.get("mahalle") or "Yaklaşık mevki")
    region = str(item.get("bolge") or item.get("bolge_anahtari") or "")
    start = str(item.get("onceki_tarih") or "?")
    end = str(item.get("son_tarih") or "?")
    scene = str(item.get("son_item") or "").strip()
    latitude = item.get("enlem")
    longitude = item.get("boylam")
    title = f"[KALIBRASYON] {key} {outcome}"
    evidence_period = f"{start} → {end}"
    if item.get("kalibrasyon_kaynagi") == "ON_SEZON_KURU_ZEMIN_GOLGE" and scene:
        evidence_period = f"güçlü ön-sezon gölge adayı · {scene}"
    body = "\n".join(
        [
            "Şantiye Radarı alarm-dışı kuru zemin kalibrasyon sonucu.",
            "Bu kayıt normal saha görevi veya alarm değildir.",
            "",
            f"Kalibrasyon: {key}",
            f"Bölge: {region}",
            f"Yaklaşık mevki: {neighborhood}",
            f"Sentinel kanıtı: {evidence_period}",
            f"Koordinat: {latitude}, {longitude}",
            f"Sonuç: {OUTCOME_LABELS.get(outcome, outcome)}",
        ]
    )
    return ISSUE_URL + "?" + urlencode({"title": title, "body": body})


def calibration_card(item):
    key = calibration_id(item)
    neighborhood = str(item.get("mahalle") or "Yaklaşık mevki")
    region = str(item.get("bolge") or "Uydu bölgesi")
    shadow = item.get("kalibrasyon_kaynagi") == "ON_SEZON_KURU_ZEMIN_GOLGE"
    try:
        area = int(float(item.get("alan_m2") or 0))
    except (TypeError, ValueError):
        area = 0
    try:
        bsi = abs(float(item.get("ortalama_bsi_degisim") or item.get("son_cift_bsi_degisim") or 0))
    except (TypeError, ValueError):
        bsi = 0.0
    try:
        rgb = float(item.get("ortalama_rgb_farki") or 0)
    except (TypeError, ValueError):
        rgb = 0.0

    with st.container(border=True):
        st.markdown(f"### 🧪 {neighborhood} · yaklaşık {area:,} m²".replace(",", "."))
        if shadow:
            scene = str(item.get("son_item") or "Sentinel sahnesi")
            st.caption(f"{region} · Kalibrasyon {key} · güçlü gölge kanıtı: {scene}")
            st.info(
                "Bu nokta güçlü **ön-sezon kuru-zemin gölge adayıdır**. Ekip gönderme "
                "talimatı değildir. Zaten yakınından geçiliyorsa gerçek nedeni etiketlemek, "
                "15 Eylül sonrası hafriyat/temel teyit geçidini saha verisiyle kalibre eder."
            )
        else:
            st.caption(
                f"{region} · Kalibrasyon {key} · "
                f"{item.get('onceki_tarih') or '?'} → {item.get('son_tarih') or '?'}"
            )
            st.info(
                "Bu nokta **alarm veya normal saha görevi değildir**. Amaç, üretim filtresinin "
                "dışında kalan kuru-zemin değişiminin gerçek hafriyat mı yoksa tarla/yol/bahçe "
                "gibi bir neden mi olduğunu öğrenmektir."
            )

        metrics = [f"BSI Δ {bsi:.3f}"]
        if rgb:
            metrics.append(f"RGB Δ {rgb:.3f}")
        locality = item.get("yerellik_orani")
        temporal = item.get("uzun_temporal_ani_baslangic_orani") or item.get(
            "zaman_serisi_ani_baslangic_orani"
        )
        try:
            if locality is not None:
                metrics.append(f"yerellik {float(locality):.2f}×")
        except (TypeError, ValueError):
            pass
        try:
            if temporal is not None:
                metrics.append(f"ani başlangıç {float(temporal):.2f}×")
        except (TypeError, ValueError):
            pass
        st.write("**Spektral/temporal kanıt:** " + " · ".join(metrics))
        st.caption(
            "Koordinat Sentinel değişim kümesinin yaklaşık merkezidir; kesin adres, ada veya parsel değildir."
        )
        route = str(item.get("harita") or "").strip()
        if route:
            st.link_button("🗺️ Yol tarifi", route, width="stretch")

        st.markdown("**Sahada gördüğün gerçek nedeni seç:**")
        columns = st.columns(4)
        for column, (outcome, label) in zip(columns, OUTCOME_BUTTONS.items()):
            column.link_button(label, issue_link(item, outcome), width="stretch")
        st.caption(
            "Seçim GitHub'da hazır bir kayıt açar. Açılan sayfada yeşil ‘Submit new issue’ "
            "düğmesine basıldığında sonuç ayrı kalibrasyon tablosuna kaydedilir; alarm istatistiğine girmez."
        )


def item_recorded(item, recorded):
    """Yeni nokta kimliğini ve eski bölge+tarih kimliğini birlikte tanı."""
    return any(key in recorded for key in calibration_id_aliases(item))


def merge_calibration_items(report_items, shadow_items):
    """Aynı kimliği iki kaynakta görürsek normal rapor kaydını tercih et."""
    merged = []
    seen = set()
    for item in list(report_items) + list(shadow_items):
        key = calibration_id(item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


st.title("🧪 Alarm Dışı Kalibrasyon Kontrolü")
st.caption(
    "Kuru-zemin körlüğünü gerçek saha verisiyle ölç. Kalibrasyon kimliği Sentinel tarih "
    "çiftinin yanı sıra yaklaşık saha noktasına da bağlıdır; böylece geri bildirim başka "
    "bir noktaya yanlışlıkla yazılmaz. Eski kayıtlar da tanınmaya devam eder."
)

report = read_report()
recorded = calibration_outcome_map()
feedback = feedback_audit_summary()
report_items = [
    item
    for item in report.get("kuru_zemin_kalibrasyon_kontrolu", []) or []
    if isinstance(item, dict)
]
shadow_items = read_shadow_items(report.get("rapor_tarihi"))
items = merge_calibration_items(report_items, shadow_items)
active = [item for item in items if not item_recorded(item, recorded)]
current_done = [item for item in items if item_recorded(item, recorded)]

m1, m2, m3, m4 = st.columns(4)
m1.metric("Bugün açık kalibrasyon", len(active))
m2.metric("Güçlü gölge adayı", len(shadow_items))
m3.metric("Bu noktalar içinde tamamlanan", len(current_done))
m4.metric("Toplam kalibrasyon sonucu", len(recorded))

st.markdown("## Saha geri bildirim kalibrasyon kilidi")
production = feedback.get("sentinel_uretim", {})
calibration_feedback = feedback.get("alarm_disi_kalibrasyon", {})
f1, f2, f3, f4 = st.columns(4)
f1.metric("Sentinel üretim etiketi", int(production.get("toplam") or 0))
f2.metric("Gerçek şantiye / kazı", int(production.get("gercek_santiye") or 0))
f3.metric("Alarm-dışı kalibrasyon", int(calibration_feedback.get("toplam") or 0))
f4.metric("Otomatik eşik değişimi", "Kapalı")

if feedback.get("manuel_inceleme_hazir"):
    st.success(
        "Saha etiketi iki sınıfta da asgari örnek seviyesine ulaştı. Boyut bandı ve uydu "
        "önceliği bazında manuel karşılaştırma yapılabilir; üretim eşikleri otomatik değişmez."
    )
else:
    st.info(str(feedback.get("neden") or "Saha etiketi henüz manuel eşik incelemesi için yeterli değil."))

st.caption(
    "Yalnız kaynağı Sentinel uydu görevi olan saha sonuçları üretim doğruluğu hesabına girer. "
    "Ön-sezon güçlü gölge etiketleri ayrı kalibrasyon tablosunda tutulur; normal saha rotası "
    "ve alarm sayısını artırmaz. 250 m² üretim alt sınırı ve 150–249 m² MİKRO ŞANTİYE "
    "diagnostik politikası bu ekranda değiştirilmez."
)

if not items:
    st.success("Bugün için güvenli alarm-dışı kuru zemin kalibrasyon noktası seçilmedi.")
elif not active:
    st.success("Bu Sentinel kanıtı için seçilen kalibrasyon kontrolleri tamamlanmış.")
else:
    for item in active:
        calibration_card(item)

st.markdown("## Kalibrasyon geçmişi")
if not recorded:
    st.info("Henüz kaydedilmiş kalibrasyon saha sonucu yok.")
else:
    rows = []
    for key, value in recorded.items():
        rows.append(
            {
                "Kalibrasyon": key,
                "Sonuç": value.get("etiket") or value.get("sonuc"),
                "Bölge": value.get("bolge") or "-",
                "Yaklaşık mevki": value.get("mahalle") or "-",
                "Alan m²": value.get("alan_m2"),
                "Sentinel": f"{value.get('onceki_tarih') or '?'} → {value.get('son_tarih') or '?'}",
                "Kayıt": value.get("kayit_zamani") or "-",
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
