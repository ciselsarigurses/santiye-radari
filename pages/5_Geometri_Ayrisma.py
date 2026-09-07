import json
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st


ROOT = Path(__file__).resolve().parents[1]
AUDIT_FILE = ROOT / "candidate_capacity_audit.json"
CHILD_AUDIT_FILE = ROOT / "diagonal_child_coordinate_audit.json"
SATELLITE_STYLE = (
    "https://raw.githubusercontent.com/ciselsarigurses/santiye-radari/"
    "main/satellite-style.json"
)


def load_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
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


def parent_rows(payload):
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
            child_text = ", ".join(f"{value} m²" for value in child_areas) or "-"
            rows.append(
                {
                    "katman": "8-KOMŞU BİRLEŞMİŞ EBEVEYN",
                    "bolge_anahtari": region_key,
                    "bolge": region.get("bolge", region_key),
                    "enlem": lat,
                    "boylam": lon,
                    "ebeveyn_alan_m2": int(round(area)),
                    "alt_kume_alan_m2": None,
                    "alt_kume_no": "-",
                    "alt_kume_sayisi": child_count,
                    "alt_alanlar": child_text,
                    "durum": "DIAGNOSTIK — ALARM DEĞİL",
                    "detay": (
                        f"8-komşu yaklaşık {int(round(area))} m² · "
                        f"4-komşu alt parçalar: {child_text}"
                    ),
                    "renk": [235, 145, 35, 210],
                    "yaricap": 190,
                    "harita": map_link(lat, lon),
                    "parsel_sorgu": parcel_link(lat, lon),
                }
            )
    return rows


def child_rows(payload):
    rows = []
    for region_key, region in (payload.get("bolgeler") or {}).items():
        if not isinstance(region, dict) or region.get("durum") != "ok":
            continue
        for parent in region.get("ebeveynler") or []:
            if not isinstance(parent, dict):
                continue
            parent_area = as_float(parent.get("sekiz_komsu_alan_m2"))
            children = [
                item for item in (parent.get("alt_adaylar") or []) if isinstance(item, dict)
            ]
            if parent_area is None or len(children) < 2:
                continue
            for index, child in enumerate(children, start=1):
                lat = as_float(child.get("enlem"))
                lon = as_float(child.get("boylam"))
                area = as_float(child.get("alan_m2"))
                if lat is None or lon is None or area is None:
                    continue
                rows.append(
                    {
                        "katman": "4-KOMŞU ALT-KÜME KOORDİNATI",
                        "bolge_anahtari": region_key,
                        "bolge": region.get("bolge", region_key),
                        "enlem": lat,
                        "boylam": lon,
                        "ebeveyn_alan_m2": int(round(parent_area)),
                        "alt_kume_alan_m2": int(round(area)),
                        "alt_kume_no": f"{index}/{len(children)}",
                        "alt_kume_sayisi": len(children),
                        "alt_alanlar": f"{int(round(area))} m²",
                        "durum": "KOORDİNAT DIAGNOSTİĞİ — ALARM DEĞİL",
                        "detay": (
                            f"4-komşu alt-küme yaklaşık {int(round(area))} m² · "
                            f"8-komşu ebeveyn yaklaşık {int(round(parent_area))} m² · "
                            f"parça {index}/{len(children)}"
                        ),
                        "renk": [45, 180, 190, 235],
                        "yaricap": 125,
                        "harita": map_link(lat, lon),
                        "parsel_sorgu": parcel_link(lat, lon),
                    }
                )
    return rows


st.set_page_config(page_title="Geometri Ayrışma", page_icon="◫", layout="wide")
st.title("◫ Geometri Ayrışma Diagnostiği")
st.caption(
    "Sentinel değişim maskesinde yalnız köşeden temas ettiği için 8-komşulukta tek "
    "küme görünen, fakat 4-komşulukta iki veya daha fazla geçerli parçaya ayrılan "
    "zemin müdahalelerini ve artık her alt parçanın ayrı değişim-pikseli temsil "
    "koordinatını haritada gösterir."
)
st.warning(
    "Bu katman alarm veya saha görevi üretmez ve 250 m² ana eşiği değiştirmez. "
    "Turuncu nokta üretimde korunan 8-komşu ebeveyn merkezidir. Camgöbeği noktalar "
    "aynı geçerli Sentinel maskesindeki 4-komşu alt-kümelerin kendi değişmiş pikselleri "
    "üzerindeki temsil noktalarıdır. Bunlar kesin parsel/adres değildir; yalnız birleşmiş "
    "ebeveyn merkezine göre saha navigasyonu için daha yerel bir geometrik referanstır."
)

payload = load_json(AUDIT_FILE)
child_payload = load_json(CHILD_AUDIT_FILE)
parents = parent_rows(payload)
children = child_rows(child_payload)
parent_frame = pd.DataFrame(parents)
child_frame = pd.DataFrame(children)
all_frame = pd.DataFrame(parents + children)

split_total = sum(
    int((region.get("baglanti_geometrisi") or {}).get("diyagonal_birlesmis_ebeveyn", 0) or 0)
    for region in (payload.get("bolgeler") or {}).values()
    if isinstance(region, dict) and region.get("durum") == "ok"
)
child_coordinate_total = sum(
    int(region.get("alt_kume_koordinat_sayisi", 0) or 0)
    for region in (child_payload.get("bolgeler") or {}).values()
    if isinstance(region, dict) and region.get("durum") == "ok"
)
uzunkuyu = (payload.get("bolgeler") or {}).get("uzunkuyu") or {}
gulbahce_region_splits = 0
if isinstance(uzunkuyu, dict):
    gulbahce_region_splits = int(
        (uzunkuyu.get("baglanti_geometrisi") or {}).get("diyagonal_birlesmis_ebeveyn", 0)
        or 0
    )

m1, m2, m3 = st.columns(3)
m1.metric("Diyagonal birleşmiş ebeveyn", split_total)
m2.metric("Ayrı alt-küme koordinatı", child_coordinate_total)
m3.metric("Uzunkuyu · Gülbahçe hattı", gulbahce_region_splits)

if split_total and not child_payload:
    st.info(
        "Diyagonal birleşme saptandı; alt-küme koordinat diagnostiği henüz veri üretmedi. "
        "Üretim ebeveyn koordinatı ve ana alarm mantığı değişmeden korunuyor."
    )
elif split_total and child_coordinate_total == 0:
    st.info(
        "Diyagonal birleşme var ancak güvenli alt-küme koordinat kaynağı bu sahne için "
        "ayrı nokta üretmedi. Ebeveyn aday otomatik bölünmedi."
    )

if all_frame.empty:
    st.success(
        "Güncel Sentinel sahnesinde haritada ayrıca incelenecek diyagonal birleşme "
        "örneği yok. Üretim 8-komşu geometrisi değiştirilmedi."
    )
else:
    layers = []
    if not parent_frame.empty:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                parent_frame,
                id="diagonal-connectivity-parent",
                get_position="[boylam,enlem]",
                get_fill_color="renk",
                get_line_color=[255, 255, 255, 255],
                get_radius="yaricap",
                radius_min_pixels=11,
                radius_max_pixels=25,
                line_width_min_pixels=3,
                stroked=True,
                pickable=True,
            )
        )
    if not child_frame.empty:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                child_frame,
                id="diagonal-connectivity-children",
                get_position="[boylam,enlem]",
                get_fill_color="renk",
                get_line_color=[255, 255, 255, 255],
                get_radius="yaricap",
                radius_min_pixels=9,
                radius_max_pixels=20,
                line_width_min_pixels=3,
                stroked=True,
                pickable=True,
            )
        )

    st.pydeck_chart(
        pdk.Deck(
            map_provider="carto",
            map_style=SATELLITE_STYLE,
            layers=layers,
            initial_view_state=pdk.ViewState(
                latitude=38.315, longitude=26.455, zoom=9.9
            ),
            tooltip={
                "html": (
                    "<b>{katman}</b><br>{bolge}<br>{detay}<br>{durum}"
                    "<br><small>Koordinat değişim geometrisi temsil noktasıdır; kesin parsel değildir.</small>"
                )
            },
        ),
        use_container_width=True,
    )
    st.caption(
        "Turuncu = üretimde korunan birleşmiş 8-komşu aday merkezi · Camgöbeği = aynı "
        "değişim maskesinde yalnız köşe teması ayrıldığında oluşan 4-komşu alt-kümenin "
        "kendi değişmiş pikseli üzerindeki temsil koordinatı. Camgöbeği noktalar yeni "
        "alarm/görev değildir; üretim 8-komşuluğunu veya 250 m² eşiğini değiştirmez."
    )
    st.dataframe(
        all_frame[
            [
                "katman",
                "bolge",
                "ebeveyn_alan_m2",
                "alt_kume_alan_m2",
                "alt_kume_no",
                "enlem",
                "boylam",
                "durum",
                "harita",
                "parsel_sorgu",
            ]
        ].rename(
            columns={
                "katman": "Katman",
                "bolge": "Bölge",
                "ebeveyn_alan_m2": "8-komşu ebeveyn (m²)",
                "alt_kume_alan_m2": "4-komşu alt-küme (m²)",
                "alt_kume_no": "Alt-küme",
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
    st.write(
        "Alt-küme koordinat denetimi:", child_payload.get("olusturma", "Veri yok")
    )
    st.write("Sentinel rapor tarihi:", payload.get("rapor_tarihi", "Veri yok"))
    st.caption(
        "4-komşu hesap yalnız geometrik bir diagnostiktir. Alt-küme temsil noktası "
        "hesaplanan bileşenin merkezine en yakın gerçek değişim pikselinden seçilir; "
        "ana 8-komşu adayın yerine geçmez. Tarla sürümü, doğal zemin, yol/altyapı, kıyı "
        "ve gerçek şantiye ayrımı için ana Sentinel spektral, temporal ve geniş-yüzey "
        "korumaları geçerliliğini korur. Ada/parsel ve hukuki statü otomatik türetilmez."
    )
