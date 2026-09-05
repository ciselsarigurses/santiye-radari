import json
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st


ROOT = Path(__file__).resolve().parents[1]
SATELLITE_STYLE = (
    "https://raw.githubusercontent.com/ciselsarigurses/santiye-radari/"
    "main/satellite-style.json"
)


def load_json(name):
    path = ROOT / name
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def map_link(lat, lon):
    return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"


def parcel_link(lat, lon):
    return f"https://parselsorgu.tkgm.gov.tr/#ara/cografi/{lat:.6f}/{lon:.6f}"


st.set_page_config(page_title="Erken Zemin Sinyalleri", page_icon="🧭", layout="wide")
st.title("🧭 Erken Zemin Sinyalleri")
st.caption(
    "Ana 250 m²+ Sentinel adaylarından ayrı olarak güçlü temporal/lokal kanıtı ve "
    "150–249 m² MİKRO ŞANTİYE izlerini tek haritada görünür tutan diagnostik ekran."
)
st.info(
    "Bu sayfa alarm veya saha görevi üretmez. Ana üretim eşiği 250 m²'dir. "
    "MİKRO ŞANTİYE katmanı 150–249 m² aralığında yalnız güçlü lokal/kompakt + "
    "temporal kanıtı olan izleri arka planda tutar."
)

temporal = load_json("temporal_local_watch.json")
micro = load_json("micro_site_watchlist.json")
gulbahce = load_json("gulbahce_coverage_guard.json")

coverage_status = gulbahce.get("durum", "veri_yok")
coverage_pct = as_float(gulbahce.get("tampon_kapsama_yuzde"))
context_pct = as_float(gulbahce.get("baglam_kapsama_yuzde"))
edge_margin = as_float(gulbahce.get("baglam_kenar_marji_m"))

m1, m2, m3, m4 = st.columns(4)
m1.metric("250–900 m² temporal-lokal", int(temporal.get("aday_sayisi", 0) or 0))
m2.metric("MİKRO güçlü güncel", int(micro.get("guncel_guclu", 0) or 0))
m3.metric("MİKRO arka plan", int(micro.get("arka_plan_takip", 0) or 0))
m4.metric(
    "Gülbahçe kapsama",
    f"%{coverage_pct:.1f}" if coverage_pct is not None else "Veri yok",
)

if coverage_status == "ok":
    st.success(
        "Gülbahçe 2 km operasyon tamponu kapsama kontrolü sağlıklı"
        + (
            f"; analiz bağlamı %{context_pct:.1f}, doğu kenar emniyet marjı "
            f"yaklaşık {edge_margin:.0f} m."
            if context_pct is not None and edge_margin is not None
            else "."
        )
    )
elif gulbahce:
    st.warning(
        "Gülbahçe kapsama denetimi sorun bildiriyor: "
        + " · ".join(str(item) for item in gulbahce.get("sorunlar", []))
    )
else:
    st.warning("Gülbahçe kapsama denetim dosyası okunamadı.")

rows = []
for candidate in temporal.get("adaylar", []):
    lat = as_float(candidate.get("enlem"))
    lon = as_float(candidate.get("boylam"))
    area = as_float(candidate.get("alan_m2"))
    if lat is None or lon is None or area is None:
        continue
    rows.append(
        {
            "katman": "250+ TEMPORAL-LOKAL",
            "durum": candidate.get("operasyonel_agirlik", "DIAGNOSTIK"),
            "bolge": candidate.get("bolge", "Mevki doğrulanmadı"),
            "mevki": candidate.get("mahalle", "Mevki doğrulanmadı"),
            "alan_m2": int(round(area)),
            "enlem": lat,
            "boylam": lon,
            "kanıt": (
                f"Yerellik {candidate.get('yerellik_orani', '-')} · "
                f"iç BSI Δ {candidate.get('ic_3x3_son_bsi_degisim', '-')}"
            ),
            "son_sahne": candidate.get("son_sentinel_item", "-"),
            "renk": [140, 70, 210, 225],
            "yaricap": 155,
            "harita": map_link(lat, lon),
            "parsel_sorgu": parcel_link(lat, lon),
        }
    )

for candidate in micro.get("adaylar", []):
    lat = as_float(candidate.get("enlem"))
    lon = as_float(candidate.get("boylam"))
    area = as_float(candidate.get("alan_m2"))
    if lat is None or lon is None or area is None:
        continue
    current = bool(candidate.get("guncel_guclu"))
    repeated = bool(candidate.get("tekrar_dogrulandi"))
    if repeated:
        status = "TEKRAR_DOGRULANDI"
        color = [30, 160, 115, 225]
    elif current:
        status = "GUNCEL_GUCLU"
        color = [35, 135, 210, 225]
    else:
        status = "ARKA_PLAN_TAKIP"
        color = [115, 125, 140, 190]
    rows.append(
        {
            "katman": "MİKRO 150–249",
            "durum": status,
            "bolge": candidate.get("bolge", "-"),
            "mevki": candidate.get("yaklasik_mevki", "Mevki doğrulanmadı"),
            "alan_m2": int(round(area)),
            "enlem": lat,
            "boylam": lon,
            "kanıt": (
                f"{candidate.get('bilesen_temporal_sinif', '-')} · "
                f"yerel kontrast {candidate.get('yerel_kontrast_orani', '-')}"
            ),
            "son_sahne": candidate.get("son_guclu_sentinel_item", "-"),
            "renk": color,
            "yaricap": 120,
            "harita": map_link(lat, lon),
            "parsel_sorgu": parcel_link(lat, lon),
        }
    )

signals = pd.DataFrame(rows)
show_background_micro = st.toggle(
    "Arka plandaki eski MİKRO izleri de göster",
    value=True,
    help=(
        "Kapalı olduğunda yalnız güncel güçlü veya farklı Sentinel sahnesinde tekrar "
        "doğrulanmış MİKRO izleri gösterilir."
    ),
)

if not signals.empty and not show_background_micro:
    signals = signals[
        ~(
            (signals["katman"] == "MİKRO 150–249")
            & (signals["durum"] == "ARKA_PLAN_TAKIP")
        )
    ].copy()

if signals.empty:
    st.info("Seçili diagnostik katmanlarda haritada gösterilecek güçlü erken sinyal yok.")
else:
    st.pydeck_chart(
        pdk.Deck(
            map_provider="carto",
            map_style=SATELLITE_STYLE,
            layers=[
                pdk.Layer(
                    "ScatterplotLayer",
                    signals,
                    id="early-ground-signals",
                    get_position="[boylam,enlem]",
                    get_fill_color="renk",
                    get_line_color=[255, 255, 255, 255],
                    get_radius="yaricap",
                    radius_min_pixels=10,
                    radius_max_pixels=24,
                    line_width_min_pixels=3,
                    stroked=True,
                    pickable=True,
                )
            ],
            initial_view_state=pdk.ViewState(
                latitude=38.315, longitude=26.455, zoom=9.9
            ),
            tooltip={
                "html": (
                    "<b>{katman}</b><br>{mevki} · yaklaşık {alan_m2} m²"
                    "<br>{durum}<br>{kanıt}"
                    "<br><small>Koordinat değişim merkezidir; kesin parsel değildir.</small>"
                )
            },
        ),
        use_container_width=True,
    )
    st.caption(
        "Mor = 250–900 m² güçlü temporal-lokal diagnostik sinyal · Mavi/yeşil = "
        "güncel/tekrar doğrulanan MİKRO iz · Gri = eski MİKRO arka plan izi. "
        "Hiçbiri bu sayfadan alarm veya saha görevi üretmez."
    )
    st.dataframe(
        signals[
            [
                "katman",
                "durum",
                "bolge",
                "mevki",
                "alan_m2",
                "enlem",
                "boylam",
                "kanıt",
                "harita",
                "parsel_sorgu",
            ]
        ].rename(
            columns={
                "katman": "Katman",
                "durum": "Durum",
                "bolge": "Bölge",
                "mevki": "Mevki",
                "alan_m2": "Alan (m²)",
                "enlem": "Enlem",
                "boylam": "Boylam",
                "kanıt": "Diagnostik kanıt",
                "harita": "Haritada aç",
                "parsel_sorgu": "TKGM manuel kontrol",
            }
        ),
        column_config={
            "Haritada aç": st.column_config.LinkColumn("Haritada aç"),
            "TKGM manuel kontrol": st.column_config.LinkColumn("TKGM manuel kontrol"),
        },
        hide_index=True,
        use_container_width=True,
    )

with st.expander("Kaynak ve güvenlik notları"):
    st.write(
        "Temporal-lokal kaynak:",
        temporal.get("kaynak_olusturma", temporal.get("rapor_tarihi", "Veri yok")),
    )
    st.write("MİKRO iz kaynak zamanı:", micro.get("olusturma", "Veri yok"))
    st.write("Gülbahçe kapsama denetimi:", gulbahce.get("olusturma", "Veri yok"))
    st.caption(
        "Ada/parsel ve hukuki statü otomatik türetilmez. TKGM bağlantısı yalnız verilen "
        "koordinatı manuel Parsel Sorgu ekranında açmak içindir."
    )
