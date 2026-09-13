import json
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_FILE = ROOT / "sentinel1_rtc_map.geojson"
SATELLITE_STYLE = (
    "https://raw.githubusercontent.com/ciselsarigurses/santiye-radari/"
    "main/satellite-style.json"
)
MAIN_MIN_M2 = 250
MICRO_RANGE_M2 = [150, 249]
BACKGROUND_LAYERS = {
    "SAR_GENIS_YUZEY_ARKA_PLAN",
    "SAR_DUSUK_KANIT_ARKA_PLAN",
    "SAR_VERI_YOK",
}


def load_snapshot():
    try:
        payload = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        return {}
    return payload


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def map_link(lat, lon):
    return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"


def parcel_link(lat, lon):
    return f"https://parselsorgu.tkgm.gov.tr/#ara/cografi/{lat:.6f}/{lon:.6f}"


def policy_ok(payload):
    return (
        payload.get("alarm") is False
        and payload.get("saha_gorevi") is False
        and int(as_float(payload.get("ana_sentinel_esigi_m2")) or 0) == MAIN_MIN_M2
        and list(payload.get("mikro_aralik_m2") or []) == MICRO_RANGE_M2
    )


def style_for_layer(layer):
    # Bu renkler yalnız görsel ayrım içindir; alarm/olasılık skoru değildir.
    styles = {
        "SAR_GUCLU_LOKAL": ([210, 45, 70, 235], 210),
        "SAR_GUCLU_LOKAL_GENIS_ARKA_PLANLI": ([190, 55, 170, 235], 210),
        "SAR_LOKAL_ORTA": ([125, 75, 210, 220], 175),
        "SAR_SAHA_ONCUL_GUCLENIYOR": ([235, 125, 25, 235], 205),
        "SAR_SAHA_ONCUL": ([235, 155, 35, 220], 170),
        "SAR_MIKRO_DIAGNOSTIK": ([30, 135, 210, 225], 150),
        "SAR_GENIS_YUZEY_ARKA_PLAN": ([110, 125, 140, 150], 135),
        "SAR_DUSUK_KANIT_ARKA_PLAN": ([145, 150, 155, 120], 115),
        "SAR_VERI_YOK": ([95, 100, 105, 105], 105),
    }
    return styles.get(layer, ([130, 130, 130, 130], 120))


def display_layer(layer):
    labels = {
        "SAR_GUCLU_LOKAL": "SAR · GÜÇLÜ KOMPAKT/LOKAL",
        "SAR_GUCLU_LOKAL_GENIS_ARKA_PLANLI": "SAR · GÜÇLÜ LOKAL · GENİŞ ARKA PLANLI",
        "SAR_LOKAL_ORTA": "SAR · LOKAL ORTA KANIT",
        "SAR_SAHA_ONCUL_GUCLENIYOR": "SAR · SAHA ÖNCÜLÜ GÜÇLENİYOR",
        "SAR_SAHA_ONCUL": "SAR · SAHA ÖNCÜLÜ İZLEME",
        "SAR_MIKRO_DIAGNOSTIK": "SAR · MİKRO 150–249 DIAGNOSTIK",
        "SAR_GENIS_YUZEY_ARKA_PLAN": "SAR · GENİŞ YÜZEY ARKA PLAN",
        "SAR_DUSUK_KANIT_ARKA_PLAN": "SAR · DÜŞÜK KANIT ARKA PLAN",
        "SAR_VERI_YOK": "SAR · METRİK YOK",
    }
    return labels.get(layer, layer or "SAR · DIAGNOSTIK")


def feature_rows(payload):
    rows = []
    for feature in payload.get("features") or []:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry") or {}
        props = feature.get("properties") or {}
        coords = geometry.get("coordinates") or []
        if geometry.get("type") != "Point" or len(coords) < 2:
            continue
        lon = as_float(coords[0])
        lat = as_float(coords[1])
        area = as_float(props.get("alan_m2"))
        if lat is None or lon is None:
            continue
        layer = str(props.get("harita_katmani") or "")
        color, radius = style_for_layer(layer)
        score = as_float(props.get("sar_lokal_degisim_skor_db"))
        env_peak = as_float(props.get("sar_cevre_degisim_tepe_db"))
        evidence = []
        if score is not None:
            evidence.append(f"lokal Δ {score:.3f} dB")
        if props.get("sar_polarizasyon_uyumu"):
            evidence.append(str(props["sar_polarizasyon_uyumu"]))
        if props.get("sar_mekansal_ayrim"):
            evidence.append(str(props["sar_mekansal_ayrim"]))
        if env_peak is not None:
            evidence.append(f"çevre tepe Δ {env_peak:.3f} dB")
        if props.get("temporal_durum"):
            evidence.append(str(props["temporal_durum"]))
        rows.append(
            {
                "katman_kodu": layer,
                "katman": display_layer(layer),
                "arka_plan": layer in BACKGROUND_LAYERS,
                "durum": "ALARM DEĞİL · DIAGNOSTIK",
                "bolge": str(props.get("bolge") or "-").upper(),
                "mevki": props.get("mahalle_yaklasik") or "Mevki doğrulanmadı",
                "alan_m2": int(round(area)) if area is not None else None,
                "enlem": lat,
                "boylam": lon,
                "sar_skor_db": score,
                "temporal": props.get("temporal_durum") or "-",
                "kanıt": " · ".join(evidence) if evidence else "SAR karşılaştırma metriği yok",
                "eski_tarih": props.get("eski_tarih") or "-",
                "yeni_tarih": props.get("yeni_tarih") or "-",
                "kaynak": props.get("sar_kaynak_tipi") or props.get("kaynak") or "-",
                "renk": color,
                "yaricap": radius,
                "harita": map_link(lat, lon),
                "parsel_sorgu": parcel_link(lat, lon),
            }
        )
    return rows


st.set_page_config(page_title="SAR Güncel Zemin", page_icon="📡", layout="wide")
st.title("📡 SAR Güncel Zemin")
st.caption(
    "Sentinel-1 RTC aynı-geometri çiftinde ölçülen kompakt/lokal geri-saçılım değişimini; "
    "MİKRO diagnostik, saha-doğrulanmış yıkım öncülü ve geniş-yüzey arka planından "
    "ayırarak Çeşme, Uzunkuyu ve Gülbahçe için tek haritada gösterir."
)
st.info(
    "Bu sayfa alarm veya saha görevi üretmez. Ana Sentinel alarm eşiği 250 m² olarak "
    "korunur; 150–249 m² MİKRO katmanı yalnız diagnostiktir. Geniş/homojen çevre, "
    "tek-polarizasyon ve düşük kanıtlı değişimler silinmez; arka planda tutulur. "
    "Koordinatlar radar örnekleme/hedef temsil noktalarıdır, kesin parsel sınırı değildir."
)

snapshot = load_snapshot()
if not snapshot:
    st.warning("Henüz kalıcı SAR harita özeti yok. Saatlik Sentinel zinciri ilk özeti ürettiğinde bu ekran dolacak.")
    st.stop()
if not policy_ok(snapshot):
    st.error("SAR harita özeti güvenlik politikasını sağlamıyor; veri haritada gösterilmedi.")
    st.stop()

rows = feature_rows(snapshot)
df = pd.DataFrame(rows)
pair_dates = snapshot.get("rtc_pair_dates") or {}
latest_dates = sorted(
    {
        str(item.get("yeni_tarih"))
        for item in pair_dates.values()
        if isinstance(item, dict) and item.get("yeni_tarih")
    }
)
latest_text = ", ".join(latest_dates) if latest_dates else "Veri yok"

strong_layers = {"SAR_GUCLU_LOKAL", "SAR_GUCLU_LOKAL_GENIS_ARKA_PLANLI"}
field_layers = {"SAR_SAHA_ONCUL", "SAR_SAHA_ONCUL_GUCLENIYOR"}

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("RTC yeni sahne", latest_text)
m2.metric("SAR hedef", len(df))
m3.metric("Güçlü kompakt/lokal", int(df["katman_kodu"].isin(strong_layers).sum()) if not df.empty else 0)
m4.metric("Saha öncülü", int(df["katman_kodu"].isin(field_layers).sum()) if not df.empty else 0)
m5.metric("MİKRO SAR diagnostik", int((df["katman_kodu"] == "SAR_MIKRO_DIAGNOSTIK").sum()) if not df.empty else 0)

if not df.empty:
    broad_bg = df[df["katman_kodu"] == "SAR_GUCLU_LOKAL_GENIS_ARKA_PLANLI"]
    if not broad_bg.empty:
        strongest = broad_bg.sort_values("sar_skor_db", ascending=False).iloc[0]
        st.warning(
            "Güçlü lokal SAR değişimi var ancak önceki dönemde geniş çevre hareketi görüldüğü için "
            "otomatik kazı alarmına yükseltilmiyor: "
            f"{strongest['bolge']} · {strongest['enlem']:.6f}, {strongest['boylam']:.6f} · "
            f"yaklaşık {strongest['alan_m2']} m² · {strongest['sar_skor_db']:.3f} dB."
        )

regions = sorted(df["bolge"].dropna().unique().tolist()) if not df.empty else []
selected_regions = st.multiselect("Bölgeler", regions, default=regions)
show_background = st.toggle(
    "Geniş-yüzey ve düşük-kanıt arka planını göster",
    value=False,
    help=(
        "Kapalıyken güçlü/lokal, saha öncülü ve MİKRO diagnostik hedefler önde kalır. "
        "Arka plan verisi silinmez; bu anahtarla tekrar görünür olur."
    ),
)

visible = df.copy()
if not visible.empty:
    visible = visible[visible["bolge"].isin(selected_regions)].copy()
    if not show_background:
        visible = visible[~visible["arka_plan"]].copy()

if visible.empty:
    st.info("Seçili filtrelerde öne çıkan SAR hedefi yok. Arka plan katmanını açarak tüm izleri görebilirsiniz.")
else:
    st.pydeck_chart(
        pdk.Deck(
            map_provider="carto",
            map_style=SATELLITE_STYLE,
            layers=[
                pdk.Layer(
                    "ScatterplotLayer",
                    visible,
                    id="sar-current-ground",
                    get_position="[boylam,enlem]",
                    get_fill_color="renk",
                    get_line_color=[255, 255, 255, 255],
                    get_radius="yaricap",
                    radius_min_pixels=10,
                    radius_max_pixels=26,
                    line_width_min_pixels=3,
                    stroked=True,
                    pickable=True,
                )
            ],
            initial_view_state=pdk.ViewState(latitude=38.31, longitude=26.46, zoom=9.8),
            tooltip={
                "html": (
                    "<b>{katman}</b><br>{bolge} · yaklaşık {alan_m2} m²"
                    "<br>{durum}<br>{kanıt}"
                    "<br><small>{eski_tarih} → {yeni_tarih}</small>"
                    "<br><small>Koordinat SAR hedef/örnekleme noktasıdır; kesin parsel değildir.</small>"
                )
            },
        ),
        use_container_width=True,
    )

    table = visible[
        [
            "katman",
            "bolge",
            "mevki",
            "alan_m2",
            "enlem",
            "boylam",
            "sar_skor_db",
            "temporal",
            "kanıt",
            "harita",
            "parsel_sorgu",
        ]
    ].rename(
        columns={
            "katman": "Katman",
            "bolge": "Bölge",
            "mevki": "Mevki",
            "alan_m2": "Alan (m²)",
            "enlem": "Enlem",
            "boylam": "Boylam",
            "sar_skor_db": "SAR lokal Δ (dB)",
            "temporal": "Temporal sınıf",
            "kanıt": "Kanıt",
            "harita": "Haritada aç",
            "parsel_sorgu": "TKGM manuel kontrol",
        }
    )
    st.dataframe(
        table,
        column_config={
            "Haritada aç": st.column_config.LinkColumn("Haritada aç"),
            "TKGM manuel kontrol": st.column_config.LinkColumn("TKGM manuel kontrol"),
        },
        hide_index=True,
        use_container_width=True,
    )

with st.expander("Kaynak ve güvenlik notları"):
    st.write("RTC çiftleri:", pair_dates or "Veri yok")
    st.write("Katman sayıları:", snapshot.get("layer_counts") or {})
    st.write("Bölge sayıları:", snapshot.get("region_counts") or {})
    st.caption(
        "Sentinel-1 geri-saçılım değişimi tek başına şantiye/kazı kanıtı değildir. "
        "Adres, ada/parsel veya hukuki statü otomatik türetilmez. TKGM bağlantısı yalnız "
        "verilen koordinatı kullanıcı tarafından manuel kontrol etmek içindir."
    )
