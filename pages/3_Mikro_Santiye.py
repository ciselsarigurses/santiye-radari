"""150-249 m² MİKRO ŞANTİYE diagnostik katmanını haritada gösterir.

Bu sayfa operasyonel alarm üretmez. Ana Sentinel alarm eşiği 250 m² olarak kalır;
MİKRO ŞANTİYE katmanı yalnız güçlü lokal/kompakt + temporal kanıtı ve arka planda
izlenen diğer mikro değişimleri ayrı bir görsel katmanda sunar.

Güçlü adayların Sentinel sahneleri arasındaki izleme hafızası da ayrıca gösterilir.
Bu hafıza, ilk güçlü konuma göre mekânsal kimlik kontrolünü ve sinyal çekirdeği
koordinat denetimini görünür kılar; hiçbir kayıt bu ekrandan saha görevine terfi etmez.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st


BASE = Path(__file__).resolve().parents[1]
SOURCE = BASE / "micro_site_footprint_priority_review.json"
WATCHLIST_SOURCE = BASE / "micro_site_watchlist.json"
COORDINATE_SOURCE = BASE / "micro_site_coordinate_audit.json"
MAIN_THRESHOLD_M2 = 250
MICRO_RANGE = (150, 249)
MAP_STYLE = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"
DEFAULT_CENTER = {"latitude": 38.31, "longitude": 26.43, "zoom": 9.5}


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
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
                "temporal": str(
                    raw.get("bilesen_temporal_sinif")
                    or raw.get("3x3_temporal_sinif")
                    or "-"
                ),
                "lokalite": str(raw.get("lokalite_sinifi") or "-"),
                "son_skor": raw.get("bilesen_son_skor"),
                "kontrast": raw.get("yerel_kontrast_orani"),
                "harita": str(
                    raw.get("harita")
                    or f"https://www.google.com/maps/search/?api=1&query={latitude},{longitude}"
                ),
                "gulbahce": bool(raw.get("gulbahce_cevre"))
                or str(raw.get("yaklasik_mevki") or "").startswith("Gülbahçe"),
            }
        )
    return pd.DataFrame(rows)


def watchlist_frame(payload: dict) -> pd.DataFrame:
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
        if raw.get("alarm") is True or raw.get("saha_gorevi") is True:
            continue

        repeated = bool(raw.get("tekrar_dogrulandi"))
        raw_repeated = bool(raw.get("tekrar_dogrulandi_ham", repeated))
        safe = raw.get("tekrar_dogrulama_mekansal_guvenli")
        current = bool(raw.get("guncel_guclu"))
        if repeated and safe is True:
            status_label = "TEKRAR · MEKÂNSAL GÜVENLİ"
            color = [31, 138, 112, 210]
            radius = 125
        elif raw_repeated and safe is False:
            status_label = "HAM TEKRAR · MEKÂNSAL BELİRSİZ"
            color = [217, 133, 35, 210]
            radius = 125
        elif current:
            status_label = "GÜÇLÜ İZ · TEK SAHNE"
            color = [126, 34, 206, 195]
            radius = 115
        else:
            status_label = "ARKA PLAN · TEMPORAL HAFIZA"
            color = [115, 115, 115, 115]
            radius = 85

        rows.append(
            {
                "latitude": latitude,
                "longitude": longitude,
                "alan_m2": int(round(area)),
                "mevki": str(raw.get("yaklasik_mevki") or "Mevki doğrulanmadı"),
                "bolge": str(raw.get("bolge") or ""),
                "mikro_iz_id": str(raw.get("mikro_iz_id") or ""),
                "status_label": status_label,
                "current": current,
                "repeated": repeated,
                "raw_repeated": raw_repeated,
                "identity_safe": safe,
                "scene_count": int(raw.get("farkli_sentinel_sahnesi_gorulme_sayisi") or 0),
                "first_date": str(raw.get("ilk_gorulme_tarihi") or "-"),
                "last_date": str(raw.get("son_guclu_gorulme_tarihi") or "-"),
                "drift_m": raw.get("ilk_konumdan_sapma_m"),
                "identity_note": str(raw.get("tekrar_dogrulama_mekansal_not") or ""),
                "color": color,
                "radius": radius,
                "harita": str(
                    raw.get("harita")
                    or f"https://www.google.com/maps/search/?api=1&query={latitude},{longitude}"
                ),
                "gulbahce": str(raw.get("yaklasik_mevki") or "").startswith("Gülbahçe"),
            }
        )
    return pd.DataFrame(rows)


def coordinate_index(payload: dict) -> dict:
    index = {}
    for raw in payload.get("adaylar") or []:
        if not isinstance(raw, dict):
            continue
        try:
            key = (
                str(raw.get("bolge") or ""),
                round(float(raw.get("enlem")), 6),
                round(float(raw.get("boylam")), 6),
            )
        except (TypeError, ValueError):
            continue
        index[key] = raw
    return index


def map_view(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return DEFAULT_CENTER
    return {
        "latitude": float(frame["latitude"].mean()),
        "longitude": float(frame["longitude"].mean()),
        "zoom": 10.2,
    }


st.set_page_config(
    page_title="MİKRO ŞANTİYE · Şantiye Radarı",
    page_icon="🔬",
    layout="wide",
)
st.title("🔬 MİKRO ŞANTİYE · Diagnostik Harita")
st.caption(
    "150–249 m² Sentinel değişimleri için ayrı teşhis katmanı. "
    "Bu ekran alarm veya saha görevi üretmez."
)

payload = load_json(SOURCE)
if not payload:
    st.warning("MİKRO ŞANTİYE diagnostik verisi henüz hazır değil.")
    st.stop()

threshold = int(payload.get("ana_uretim_esigi_m2") or 0)
interval = tuple(payload.get("mikro_aralik_m2") or ())
if threshold != MAIN_THRESHOLD_M2 or interval != MICRO_RANGE:
    st.error(
        "Diagnostik katmanın eşik invariantı bozulmuş görünüyor. Harita yalnız "
        "250 m² ana eşik ve 150–249 m² mikro bant doğrulandığında gösterilir."
    )
    st.stop()

frame = candidate_frame(payload)
watch_payload = load_json(WATCHLIST_SOURCE)
watch_frame = watchlist_frame(watch_payload) if watch_payload else pd.DataFrame()
coord_payload = load_json(COORDINATE_SOURCE)
coord_index = coordinate_index(coord_payload) if coord_payload else {}

strong_count = int(frame["strong"].sum()) if not frame.empty else 0
background_count = int(frame["broad"].sum()) if not frame.empty else 0
gulbahce_total = int(frame["gulbahce"].sum()) if not frame.empty else 0
gulbahce_strong = (
    int((frame["gulbahce"] & frame["strong"]).sum()) if not frame.empty else 0
)

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Ana alarm eşiği", "250 m²")
m2.metric("Mikro aday", len(frame))
m3.metric("Güçlü diagnostik", strong_count)
m4.metric("Arka plan geniş yüzey", background_count)
m5.metric("Gülbahçe güçlü / toplam", f"{gulbahce_strong} / {gulbahce_total}")

st.info(
    "Mor: lokal/kompakt + temporal kanıtı birlikte güçlü mikro diagnostik. "
    "Mavi: kanıtın bir kısmı var, izleniyor. Gri: geniş/homojen hareket riski; "
    "silinmedi, yalnız arka planda tutuluyor. Hiçbiri operasyonel kırmızı/turuncu "
    "saha alarmı değildir."
)

show_background = st.checkbox(
    "Arka plan ve bekleyen mikro değişimleri de göster",
    value=False,
)
map_rows = (
    frame.copy()
    if show_background or frame.empty
    else frame[frame["strong"]].copy()
)

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
        "Bu liste saha görevi değildir. Yeni Sentinel sahnesinde tekrar görülme veya "
        "güvenilir ek yapılaşma kanıtı aranır."
    )
    for idx, row in frame[frame["strong"]].reset_index(drop=True).iterrows():
        with st.expander(f"#{idx + 1} · {row['mevki']} · {row['alan_m2']} m²"):
            st.write(
                "**Koordinat:**",
                f"{row['latitude']:.6f}, {row['longitude']:.6f}",
            )
            st.write("**Temporal kanıt:**", row["temporal"])
            st.write("**Lokalite:**", row["lokalite"])
            if pd.notna(row["son_skor"]):
                st.write("**Son bileşen skoru:**", row["son_skor"])
            if pd.notna(row["kontrast"]):
                st.write("**Yerel kontrast oranı:**", row["kontrast"])

            watch_match = pd.DataFrame()
            if not watch_frame.empty:
                watch_match = watch_frame[
                    (watch_frame["bolge"] == row["bolge"])
                    & ((watch_frame["latitude"] - row["latitude"]).abs() < 0.00001)
                    & ((watch_frame["longitude"] - row["longitude"]).abs() < 0.00001)
                ]
            if not watch_match.empty:
                trace = watch_match.iloc[0]
                st.write(
                    "**Temporal izleme hafızası:**",
                    f"{trace['scene_count']} farklı Sentinel sahnesi · {trace['status_label']}",
                )
                if pd.notna(trace["drift_m"]):
                    st.write(
                        "**İlk güçlü konumdan sapma:**",
                        f"{float(trace['drift_m']):.1f} m",
                    )
                if trace["identity_note"]:
                    st.caption(trace["identity_note"])

            coord = coord_index.get(
                (
                    str(row["bolge"]),
                    round(float(row["latitude"]), 6),
                    round(float(row["longitude"]), 6),
                )
            )
            if coord:
                st.write(
                    "**Sinyal çekirdeği koordinat kalitesi:**",
                    str(coord.get("koordinat_kalitesi") or "-"),
                )
                st.write(
                    "**Temsilci → çekirdek sapması:**",
                    f"{float(coord.get('sinyal_cekirdegi_sapma_m') or 0.0):.1f} m",
                )

            st.write("**Karar:**", row["karar_nedeni"])
            st.link_button(
                "📍 Koordinatı haritada aç",
                row["harita"],
                use_container_width=True,
            )
else:
    st.warning("Şu anda güçlü MİKRO ŞANTİYE diagnostik adayı yok.")

st.divider()
st.subheader("Temporal izleme hafızası")
st.caption(
    "Güçlü mikro aday ilk sahneden sonra ana değişim maskesinden düşse bile burada "
    "arka planda korunur. Ham tekrar iki farklı Sentinel sahnesinde güçlü görünümü "
    "ifade eder; geçerli tekrar için ayrıca ilk güçlü konuma göre en fazla 25 m "
    "mekânsal kimlik şartı aranır."
)

if watch_frame.empty:
    st.info("Temporal izleme hafızasında kayıt yok.")
else:
    safe_repeat_count = int(watch_frame["repeated"].sum())
    ambiguous_repeat_count = int(
        (watch_frame["raw_repeated"] & (watch_frame["identity_safe"] == False)).sum()
    )
    current_trace_count = int(watch_frame["current"].sum())
    background_trace_count = len(watch_frame) - current_trace_count
    gulbahce_trace_count = int(watch_frame["gulbahce"].sum())

    w1, w2, w3, w4, w5 = st.columns(5)
    w1.metric("Güncel güçlü iz", current_trace_count)
    w2.metric("Güvenli tekrar", safe_repeat_count)
    w3.metric("Belirsiz ham tekrar", ambiguous_repeat_count)
    w4.metric("Arka plan iz", background_trace_count)
    w5.metric("Gülbahçe iz", gulbahce_trace_count)

    show_temporal_background = st.checkbox(
        "Temporal hafızadaki arka plan izlerini haritada göster",
        value=True,
    )
    temporal_rows = (
        watch_frame.copy()
        if show_temporal_background
        else watch_frame[
            watch_frame["current"] | watch_frame["repeated"] | watch_frame["raw_repeated"]
        ].copy()
    )

    if not temporal_rows.empty:
        view = map_view(temporal_rows)
        temporal_layer = pdk.Layer(
            "ScatterplotLayer",
            data=temporal_rows,
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
        temporal_deck = pdk.Deck(
            map_style=MAP_STYLE,
            initial_view_state=pdk.ViewState(**view),
            layers=[temporal_layer],
            tooltip={
                "html": (
                    "<b>{status_label}</b><br/>"
                    "{mevki} · {alan_m2} m²<br/>"
                    "Sentinel sahnesi: {scene_count}<br/>"
                    "İlk güçlü: {first_date}<br/>"
                    "Son güçlü: {last_date}<br/>"
                    "İlk konumdan sapma: {drift_m} m"
                ),
                "style": {"backgroundColor": "white", "color": "black"},
            },
        )
        st.pydeck_chart(temporal_deck, use_container_width=True)

st.divider()
st.caption(
    "15 Eylül sonrası operasyonel ağırlık yalnız yeni Sentinel görüntüsünde tekrar "
    "doğrulanan hafriyat/kazı sinyallerinde artırılır. Bu katman 250 m² ana alarm "
    "eşiğini düşürmez ve tarla/sit/tarım statüsünü tek başına eleme nedeni olarak "
    "kullanmaz."
)
