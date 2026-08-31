from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import streamlit as st

from calibration_outcome import calibration_id, calibration_id_aliases, calibration_outcome_map
from field_outcome import OUTCOME_LABELS


st.set_page_config(page_title="Kalibrasyon", page_icon="🧪", layout="wide")

REPORT_FILE = Path(__file__).resolve().parents[1] / "latest_report.json"
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


def issue_link(item, outcome):
    key = calibration_id(item)
    neighborhood = str(item.get("mahalle") or "Yaklaşık mevki")
    region = str(item.get("bolge") or item.get("bolge_anahtari") or "")
    start = str(item.get("onceki_tarih") or "?")
    end = str(item.get("son_tarih") or "?")
    latitude = item.get("enlem")
    longitude = item.get("boylam")
    title = f"[KALIBRASYON] {key} {outcome}"
    body = "\n".join(
        [
            "Şantiye Radarı alarm-dışı kuru zemin kalibrasyon sonucu.",
            "Bu kayıt normal saha görevi veya alarm değildir.",
            "",
            f"Kalibrasyon: {key}",
            f"Bölge: {region}",
            f"Yaklaşık mevki: {neighborhood}",
            f"Sentinel çifti: {start} → {end}",
            f"Koordinat: {latitude}, {longitude}",
            f"Sonuç: {OUTCOME_LABELS.get(outcome, outcome)}",
        ]
    )
    return ISSUE_URL + "?" + urlencode({"title": title, "body": body})


def calibration_card(item):
    key = calibration_id(item)
    neighborhood = str(item.get("mahalle") or "Yaklaşık mevki")
    region = str(item.get("bolge") or "Uydu bölgesi")
    try:
        area = int(float(item.get("alan_m2") or 0))
    except (TypeError, ValueError):
        area = 0
    try:
        bsi = abs(float(item.get("ortalama_bsi_degisim") or 0))
    except (TypeError, ValueError):
        bsi = 0.0
    try:
        rgb = float(item.get("ortalama_rgb_farki") or 0)
    except (TypeError, ValueError):
        rgb = 0.0

    with st.container(border=True):
        st.markdown(f"### 🧪 {neighborhood} · yaklaşık {area:,} m²".replace(",", "."))
        st.caption(
            f"{region} · Kalibrasyon {key} · "
            f"{item.get('onceki_tarih') or '?'} → {item.get('son_tarih') or '?'}"
        )
        st.info(
            "Bu nokta **alarm veya normal saha görevi değildir**. Amaç, üretim filtresinin "
            "dışında kalan kuru-zemin değişiminin gerçek hafriyat mı yoksa tarla/yol/bahçe "
            "gibi bir neden mi olduğunu öğrenmektir."
        )
        st.write(f"**Spektral değişim:** BSI Δ {bsi:.3f} · RGB Δ {rgb:.3f}")
        st.caption(
            "Koordinat Sentinel değişim kümesinin yaklaşık merkezidir; kesin adres, ada veya parsel değildir."
        )
        route = str(item.get("harita") or "").strip()
        if route:
            st.link_button("🗺️ Yol tarifi", route, width="stretch")

        st.markdown("**Sahada gördüğün gerçek nedeni seç:**")
        columns = st.columns(4)
        for column, (outcome, label) in zip(columns, OUTCOME_BUTTONS.items()):
            column.link_button(
                label,
                issue_link(item, outcome),
                width="stretch",
            )
        st.caption(
            "Seçim GitHub'da hazır bir kayıt açar. Açılan sayfada yeşil ‘Submit new issue’ "
            "düğmesine basıldığında sonuç ayrı kalibrasyon tablosuna kaydedilir; alarm istatistiğine girmez."
        )


def item_recorded(item, recorded):
    """Yeni nokta kimliğini ve eski bölge+tarih kimliğini birlikte tanı."""
    return any(key in recorded for key in calibration_id_aliases(item))


st.title("🧪 Alarm Dışı Kalibrasyon Kontrolü")
st.caption(
    "Kuru-zemin körlüğünü gerçek saha verisiyle ölç. Kalibrasyon kimliği artık Sentinel tarih "
    "çiftinin yanı sıra yaklaşık saha noktasına da bağlıdır; böylece geri bildirim başka bir "
    "noktaya yanlışlıkla yazılmaz. Eski kayıtlar da tanınmaya devam eder."
)

report = read_report()
recorded = calibration_outcome_map()
items = [
    item
    for item in report.get("kuru_zemin_kalibrasyon_kontrolu", []) or []
    if isinstance(item, dict)
]
active = [item for item in items if not item_recorded(item, recorded)]
current_done = [item for item in items if item_recorded(item, recorded)]

m1, m2, m3 = st.columns(3)
m1.metric("Bugün açık kalibrasyon", len(active))
m2.metric("Bu noktalar içinde tamamlanan", len(current_done))
m3.metric("Toplam kalibrasyon sonucu", len(recorded))

if not items:
    st.success("Bugün için güvenli alarm-dışı kuru zemin kalibrasyon noktası seçilmedi.")
elif not active:
    st.success("Bu Sentinel görüntü çifti için seçilen kalibrasyon kontrolleri tamamlanmış.")
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
                "Sentinel": (
                    f"{value.get('onceki_tarih') or '?'} → {value.get('son_tarih') or '?'}"
                ),
                "Kayıt": value.get("kayit_zamani") or "-",
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")