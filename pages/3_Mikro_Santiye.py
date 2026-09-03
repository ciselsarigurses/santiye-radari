"""150-249 m² MİKRO ŞANTİYE diagnostik katmanını haritada gösterir.

Bu sayfa operasyonel alarm üretmez. Ana Sentinel alarm eşiği 250 m² olarak kalır;
MİKRO ŞANTİYE katmanı yalnız güçlü lokal/kompakt + temporal kanıtı ve arka planda
izlenen diğer mikro değişimleri ayrı bir görsel katmanda sunar.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st


SOURCE = Path(__file__).resolve().parents[1] / "micro_site_footprint_priority_review.json"
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE = (150, 249)
MAP_STYLE = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"
DEFAULT_CENTER = {"latitude": 38.31, "longitude": 26.43, "zoom": 9.5}


def load_review() -> dict:
    if not SOURCE.exists():
        return {}
    try:
        payload = json.loads(SOURCE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def candidate_frame(payload: dict) -> pd.DataFrame:
    rows = []
    for raw in payload.get("adaylar") or []:
        if not isinstance(raw, dict):
            continue
        try:
            latitude = float(raw.get("enlem"))
            longitude = float(raw.get("boylam"))
            area = float(raw.get("alan_m2"))
        except (TypeError, ValueError):
            continue
        if not (MICRO_RANGE[0] <= area <= MICRO_RANGE[1]):
            continue

        label = str(raw.get("karar_sinifi") or "BEKLE")
        strong = bool(raw.get("mikro_footprint_guclu_diagnostik"))
        broad = bool(raw.get("genis_yuzey_riski")) or label == "GENIS_HAREKET_ARKA_PLAN"
        if strong:
            layer_group = "GÜÇLÜ MİKRO DIAGNOSTİK"
            color = [126, 34, 206, 220]
            radius = 120
        elif broad:
            layer_group = "ARKA PLAN · GENİŞ YÜZEY"
            color = [115, 115, 115, 120]
            radius = 80
        else:
            layer_group = "İZLE · KANIT TAM DEĞİL"
            color = [35, 111, 190, 150]
            radius = 90

        rows.append(
            {
                "latitude": latitude,
                "longitude": longitude,
                "alan_m2": int(round(area)),
                "mevki": str(raw.get("yaklasik_mevki") or "Mevki doğrulanmadı"),
                "bolge": str(raw.get("bolge") or ""),
                "karar_sinifi": label,
                "karar_nedeni": str(raw.get("karar_nedeni") or raw.get("neden") or ""),
                "layer_group": layer_group,
                "strong": strong,
                "broad": broad,
                "color": color,
                "radius": radius,
                "temporal": str(raw.get("bilesen_temporal_sinif") or raw.get("3x3_temporal_sinif") or "-"),
                "lokalite": str(raw.get("lokalite_sinifi") or "-"),
                "son_skor": raw.get("bilesen_son_skor"),
                "kontrast": raw.get("yerel_kontrast_orani"),
                "harita": str(raw.get("harita") or f"https://www.google.com/maps/search/?api=1&query={latitude},{longitude}"),
                "gulbahce": bool(raw.get("gulbahce_cevre")) or str(raw.get("yaklasik_mevki") or "").startswith("Gülbahçe"),
            }
        )
    return pd.DataFrame(rows)


def map_view(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return DEFAULT_CENTER
    return {
        "latitude": float(frame["latitude"].mean()),
        "longitude": float(frame["longitude"].mean()),
        "zoom": 10.2,
    }


st.set_page_config(page_title="MİKRO ŞANTİYE · Şantiye Radarı", page_icon="🔬", layout="wide")
st.title("🔬 MİKRO ŞANTİYE · Diagnostik Harita")
st.caption(
    "150–249 m² Sentinel değişimleri için ayrı teşhis katmanı. Bu ekran alarm veya saha görevi üretmez."
)

payload = load_review()
if not payload:
    st.warning("MİKRO ŞANTİYE diagnostik verisi henüz hazır değil.")
    st.stop()

threshold = int(payload.get("ana_uretim_esigi_m2") or 0)
interval = tuple(payload.get("mikro_aralik_m2") or ())
if threshold != MAIN_THRESHOLD_M2 or interval != MICRO_RANGE:
    st.error(
        "Diagnostik katmanın eşik invariantı bozulmuş görünüyor. Harita yalnız 250 m² ana eşik ve 150–249 m² mikro bant doğrulandığında gösterilir."
    )
    st.stop()

frame = candidate_frame(payload)
strong_count = int(frame["strong"].sum()) if not frame.empty else 0
background_count = int(frame["broad"].sum()) if not frame.empty else 0
gulbahce_total = int(frame["gulbahce"].sum()) if not frame.empty else 0
gulbahce_strong = int((frame["gulbahce"] & frame["strong"]).sum()) if not frame.empty else 0

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Ana alarm eşiği", "250 m²")
m2.metric("Mikro aday", len(frame))
m3.metric("Güçlü diagnostik", strong_count)
m4.metric("Arka plan geniş yüzey", background_count)
m5.metric("Gülbahçe güçlü / toplam", f"{gulbahce_strong} / {gulbahce_total}")

st.info(
    "Mor: lokal/kompakt + temporal kanıtı birlikte güçlü mikro diagnostik. "
    "Mavi: kanıtın bir kısmı var, izleniyor. Gri: geniş/homojen hareket riski; "
    "silinmedi, yalnız arka planda tutuluyor. Hiçbiri operasyonel kırmızı/turuncu saha alarmı değildir."
)

show_background = st.checkbox("Arka plan ve bekleyen mikro değişimleri de göster", value=False)
map_rows = frame.copy() if show_background else frame[frame["strong"]].copy()

if map_rows.empty:
    st.info("Seçili katmanda gösterilecek aday yok.")
else:
    view = map_view(map_rows)
    layer = pdk.Layer(
        "ScatterplotLayer",
        data=map_rows,
        get_position="[longitude, latitude]",
        get_fill_color="color",
        get_line_color=[40, 40, 40, 180],
        get_radius="radius",
        radius_min_pixels=7,
        radius_max_pixels=18,
        stroked=True,
        line_width_min_pixels=1,
        pickable=True,
    )
    deck = pdk.Deck(
        map_style=MAP_STYLE,
        initial_view_state=pdk.ViewState(**view),
        layers=[layer],
        tooltip={
            "html": (
                "<b>{layer_group}</b><br/>"
                "{mevki} · {alan_m2} m²<br/>"
                "Temporal: {temporal}<br/>"
                "Lokalite: {lokalite}<br/>"
                "{karar_nedeni}"
            ),
            "style": {"backgroundColor": "white", "color": "black"},
        },
    )
    st.pydeck_chart(deck, use_container_width=True)

if strong_count:
    st.subheader("Öncelikli tekrar doğrulama adayları")
    st.caption(
        "Bu liste saha görevi değildir. Yeni Sentinel sahnesinde tekrar görülme veya güvenilir ek yapılaşma kanıtı aranır."
    )
    for idx, row in frame[frame["strong"]].reset_index(drop=True).iterrows():
        with st.expander(f"#{idx + 1} · {row['mevki']} · {row['alan_m2']} m²"):
            st.write("**Koordinat:**", f"{row['latitude']:.6f}, {row['longitude']:.6f}")
            st.write("**Temporal kanıt:**", row["temporal"])
            st.write("**Lokalite:**", row["lokalite"])
            if pd.notna(row["son_skor"]):
                st.write("**Son bileşen skoru:**", row["son_skor"])
            if pd.notna(row["kontrast"]):
                st.write("**Yerel kontrast oranı:**", row["kontrast"])
            st.write("**Karar:**", row["karar_nedeni"])
            st.link_button("📍 Koordinatı haritada aç", row["harita"], use_container_width=True)
else:
    st.warning("Şu anda güçlü MİKRO ŞANTİYE diagnostik adayı yok.")

st.divider()
st.caption(
    "15 Eylül sonrası operasyonel ağırlık yalnız yeni Sentinel görüntüsünde tekrar doğrulanan hafriyat/kazı sinyallerinde artırılır. "
    "Bu katman 250 m² ana alarm eşiğini düşürmez ve tarla/sit/tarım statüsünü tek başına eleme nedeni olarak kullanmaz."
)
