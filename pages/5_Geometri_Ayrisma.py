import json
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st


ROOT = Path(__file__).resolve().parents[1]
AUDIT_FILE = ROOT / "candidate_capacity_audit.json"
SATELLITE_STYLE = (
    "https://raw.githubusercontent.com/ciselsarigurses/santiye-radari/"
    "main/satellite-style.json"
)


def load_audit():
    try:
        data = json.loads(AUDIT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
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


def diagnostic_rows(payload):
    rows = []
    for region_key, region in (payload.get("bolgeler") or {}).items():
        if not isinstance(region, dict) or region.get("durum") != "ok":
            continue
        geometry = region.get("baglanti_geometrisi") or {}
        for example in geometry.get("ornekler") or []:
            if not isinstance(example, dict):
                continue
            try:
                child_count = int(example.get("dort_komsu_alt_aday_sayisi") or 0)
            except (TypeError, ValueError):
                child_count = 0
            if child_count < 2:
                continue
            lat = as_float(example.get("enlem"))
            lon = as_float(example.get("boylam"))
            area = as_float(example.get("sekiz_komsu_alan_m2"))
            if lat is None or lon is None or area is None:
                continue
            child_areas = []
            for value in example.get("dort_komsu_alt_alanlar_m2") or []:
                try:
                    child_areas.append(int(round(float(value))))
                except (TypeError, ValueError):
                    continue
            rows.append(
                {
                    "bolge_anahtari": region_key,
                    "bolge": region.get("bolge", region_key),
                    "enlem": lat,
                    "boylam": lon,
                    "sekiz_komsu_alan_m2": int(round(area)),
                    "dort_komsu_alt_aday_sayisi": child_count,
                    "alt_alanlar": ", ".join(f"{value} m²" for value in child_areas) or "-",
                    "durum": "DIAGNOSTIK — ALARM DEGIL",
                    "harita": map_link(lat, lon),
                    "parsel_sorgu": parcel_link(lat, lon),
                }
            )
    return rows


st.set_page_config(page_title="Geometri Ayrışma", page_icon="◫", layout="wide")
st.title("◫ Geometri Ayrışma Diagnostiği")
st.caption(
    "Sentinel değişim maskesinde yalnız köşeden temas ettiği için 8-komşulukta tek "
    "küme görünen, fakat 4-komşulukta iki veya daha fazla ayrı şantiye-ölçeği parçaya "
    "ayrılabilen noktaları haritada görünür tutar."
)
st.warning(
    "Bu katman alarm veya saha görevi üretmez ve 250 m² ana eşiği değiştirmez. "
    "Turuncu nokta, birleşmiş 8-komşu kümenin yaklaşık merkezidir; alt parçaların "
    "kesin koordinatı veya parseli değildir. Amaç tek bir geniş merkez koordinatının "
    "yakındaki iki ayrı zemin müdahalesini gizleyebileceği yerleri operatöre göstermektir."
)

payload = load_audit()
rows = diagnostic_rows(payload)
frame = pd.DataFrame(rows)

split_total = sum(
    int((region.get("baglanti_geometrisi") or {}).get("diyagonal_birlesmis_ebeveyn", 0) or 0)
    for region in (payload.get("bolgeler") or {}).values()
    if isinstance(region, dict) and region.get("durum") == "ok"
)
recovered_total = sum(
    int((region.get("baglanti_geometrisi") or {}).get("ayrisan_kucuk_250_800", 0) or 0)
    + int((region.get("baglanti_geometrisi") or {}).get("ayrisan_santiye_olcegi_800_10000", 0) or 0)
    for region in (payload.get("bolgeler") or {}).values()
    if isinstance(region, dict) and region.get("durum") == "ok"
)
gulbahce_region_splits = 0
uzunkuyu = (payload.get("bolgeler") or {}).get("uzunkuyu") or {}
if isinstance(uzunkuyu, dict):
    gulbahce_region_splits = int(
        (uzunkuyu.get("baglanti_geometrisi") or {}).get("diyagonal_birlesmis_ebeveyn", 0)
        or 0
    )

m1, m2, m3 = st.columns(3)
m1.metric("Diyagonal birleşmiş küme", split_total)
m2.metric("4-komşuda ayrışan 250–10.000 m² parça", recovered_total)
m3.metric("Uzunkuyu · Gülbahçe hattı", gulbahce_region_splits)

if frame.empty:
    st.success(
        "Güncel Sentinel sahnesinde haritada ayrıca incelenecek diyagonal birleşme "
        "örneği yok. Üretim 8-komşu geometrisi değiştirilmedi."
    )
else:
    st.pydeck_chart(
        pdk.Deck(
            map_provider="carto",
            map_style=SATELLITE_STYLE,
            layers=[
                pdk.Layer(
                    "ScatterplotLayer",
                    frame,
                    id="diagonal-connectivity-diagnostic",
                    get_position="[boylam,enlem]",
                    get_fill_color=[235, 145, 35, 210],
                    get_line_color=[255, 255, 255, 255],
                    get_radius=190,
                    radius_min_pixels=11,
                    radius_max_pixels=25,
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
                    "<b>Geometri ayrışma diagnostiği</b><br>{bolge}"
                    "<br>8-komşu: yaklaşık {sekiz_komsu_alan_m2} m²"
                    "<br>4-komşu alt parça: {dort_komsu_alt_aday_sayisi}"
                    "<br>Alt alanlar: {alt_alanlar}"
                    "<br><small>Alarm/görev değildir; merkez yaklaşık noktadır.</small>"
                )
            },
        ),
        use_container_width=True,
    )
    st.caption(
        "Turuncu = aynı değişim maskesinde yalnız diyagonal temas nedeniyle birleşmiş "
        "8-komşu küme. Saha ekibi bu noktayı ancak mevcut rota üzerinde yakınından "
        "geçiyorsa çevresel gözlem için kullanmalı; otomatik görev açılmaz."
    )
    st.dataframe(
        frame[
            [
                "bolge",
                "sekiz_komsu_alan_m2",
                "dort_komsu_alt_aday_sayisi",
                "alt_alanlar",
                "enlem",
                "boylam",
                "durum",
                "harita",
                "parsel_sorgu",
            ]
        ].rename(
            columns={
                "bolge": "Bölge",
                "sekiz_komsu_alan_m2": "8-komşu alan (m²)",
                "dort_komsu_alt_aday_sayisi": "4-komşu parça",
                "alt_alanlar": "Alt parça alanları",
                "enlem": "Enlem",
                "boylam": "Boylam",
                "durum": "Durum",
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

with st.expander("Kaynak ve yorumlama sınırları"):
    st.write("Kapasite/geometri denetimi:", payload.get("olusturma", "Veri yok"))
    st.write("Sentinel rapor tarihi:", payload.get("rapor_tarihi", "Veri yok"))
    st.caption(
        "4-komşu hesap yalnız geometrik bir diagnostiktir. Tarla sürümü, doğal zemin, "
        "yol/altyapı veya gerçek şantiye ayrımı için ana Sentinel spektral, temporal, "
        "kıyı ve geniş-yüzey korumaları geçerliliğini korur. Ada/parsel ve hukuki statü "
        "otomatik türetilmez."
    )
