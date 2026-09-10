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
MAIN_MIN_M2 = 250
MICRO_MIN_M2 = 150
MICRO_MAX_M2 = 249
CURRENT_BLIND_MAX_M2 = 6500


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


def current_blind_rows(payload):
    """Yalnız en yeni sahnede kör, tarihsel kara kanıtlı 250–6500 m² kümeleri döndür."""
    if payload.get("durum") != "ok" or payload.get("ayni_sentinel_sahnesi") is not True:
        return []
    rows = []
    for item in payload.get("guncel_sahne_kor_kumeleri") or []:
        if not isinstance(item, dict):
            continue
        area = as_float(item.get("alan_m2"))
        lat = as_float(item.get("enlem"))
        lon = as_float(item.get("boylam"))
        if area is None or lat is None or lon is None:
            continue
        if not (MAIN_MIN_M2 <= area <= CURRENT_BLIND_MAX_M2):
            continue
        if str(item.get("yuzey_kaniti") or "") != "TARIHSEL_KARA":
            continue
        rows.append(
            {
                "katman": "GÜNCEL SAHNE KÖR 250–6500",
                "durum": "DEVRIYE_KAPSIYOR" if item.get("secilen_devriye_kapsiyor") is True else "KÖR · ARKA PLAN",
                "alan_m2": int(round(area)),
                "enlem": lat,
                "boylam": lon,
                "neden": str(item.get("neden") or "KALITE_GECERSIZ"),
                "kanıt": "Tarihsel açık sahnelerde kara; en yeni Sentinel sahnesinde kalite nedeniyle görünmüyor",
                "devriye_mesafe_m": item.get("secilen_devriyeye_mesafe_m"),
                "renk": [235, 145, 40, 205],
                "yaricap": 190,
                "harita": map_link(lat, lon),
                "parsel_sorgu": parcel_link(lat, lon),
            }
        )
    return rows


def water_transition_rows(payload):
    """Tüm güncel kara→SCL-su geçişlerini alarmdan ayrı arka plan katmanları olarak göster."""
    rows = []
    for item in payload.get("izler") or []:
        if not isinstance(item, dict):
            continue
        # Şema v2 genel geçiş bayrağını kullanır. Eski dosyalar için yalnız kompakt
        # iç-kara belirsizliği görünür kalır; alarm veya görev davranışı değişmez.
        is_current_transition = item.get("guncel_scl_su_gecisi") is True
        if not is_current_transition and item.get("guncel_scl_su_belirsizligi") is not True:
            continue
        area = as_float(item.get("alan_m2"))
        lat = as_float(item.get("enlem"))
        lon = as_float(item.get("boylam"))
        if area is None or lat is None or lon is None:
            continue

        source_class = str(item.get("kaynak_sinif") or "")
        if item.get("guncel_scl_su_belirsizligi") is True:
            layer_name = "SCL SU BELİRSİZLİĞİ · İÇ KARA"
            display_reason = str(item.get("durum") or "GUNCEL_SCL_SU_BELIRSIZLIGI")
            color = [70, 135, 190, 175]
            radius = 145
        elif source_class == "KIYI_SU_ARKA_PLAN":
            layer_name = "KIYI/SU · ARKA PLAN"
            display_reason = "Tarihsel suya yakın; şantiye alarmından ayrıldı"
            color = [105, 135, 160, 120]
            radius = 125
        elif source_class == "GENIS_SU_YUZEY_ARKA_PLAN":
            layer_name = "GENİŞ SU/YÜZEY · ARKA PLAN"
            display_reason = "Geniş/homojen yüzey hareketi; şantiye alarmından ayrıldı"
            color = [90, 115, 140, 105]
            radius = 170
        else:
            layer_name = "SCL SU GEÇİŞİ · ARKA PLAN"
            display_reason = str(item.get("durum") or "ARKA_PLAN_TAKIP")
            color = [100, 125, 150, 115]
            radius = 130

        seen_count = int(item.get("farkli_sentinel_sahnesi_gorulme_sayisi") or 0)
        rows.append(
            {
                "katman": layer_name,
                "durum": "ALARM DEĞİL",
                "alan_m2": int(round(area)),
                "enlem": lat,
                "boylam": lon,
                "neden": display_reason,
                "kanıt": f"Tarihsel kara → güncel SCL=su; {seen_count} farklı Sentinel sahnesi",
                "devriye_mesafe_m": None,
                "renk": color,
                "yaricap": radius,
                "harita": map_link(lat, lon),
                "parsel_sorgu": parcel_link(lat, lon),
            }
        )
    return rows


def _self_check():
    sample = {
        "durum": "ok",
        "ayni_sentinel_sahnesi": True,
        "guncel_sahne_kor_kumeleri": [
            {"enlem": 38.34, "boylam": 26.64, "alan_m2": 400, "yuzey_kaniti": "TARIHSEL_KARA"},
            {"enlem": 38.35, "boylam": 26.65, "alan_m2": 200, "yuzey_kaniti": "TARIHSEL_KARA"},
            {"enlem": 38.36, "boylam": 26.66, "alan_m2": 7000, "yuzey_kaniti": "TARIHSEL_KARA"},
            {"enlem": 38.37, "boylam": 26.67, "alan_m2": 400, "yuzey_kaniti": "TARIHSEL_SU"},
        ],
    }
    selected = current_blind_rows(sample)
    assert len(selected) == 1 and selected[0]["alan_m2"] == 400
    assert MAIN_MIN_M2 == 250
    assert (MICRO_MIN_M2, MICRO_MAX_M2) == (150, 249)

    water_sample = {
        "izler": [
            {
                "enlem": 38.33,
                "boylam": 26.65,
                "alan_m2": 400,
                "guncel_scl_su_gecisi": True,
                "guncel_scl_su_belirsizligi": True,
                "kaynak_sinif": "IC_KARA_SCL_SU_BELIRSIZLIGI",
                "farkli_sentinel_sahnesi_gorulme_sayisi": 1,
            },
            {
                "enlem": 38.34,
                "boylam": 26.66,
                "alan_m2": 800,
                "guncel_scl_su_gecisi": True,
                "guncel_scl_su_belirsizligi": False,
                "kaynak_sinif": "KIYI_SU_ARKA_PLAN",
                "farkli_sentinel_sahnesi_gorulme_sayisi": 1,
            },
            {
                "enlem": 38.35,
                "boylam": 26.67,
                "alan_m2": 900,
                "guncel_scl_su_gecisi": False,
                "guncel_scl_su_belirsizligi": False,
                "kaynak_sinif": "KIYI_SU_ARKA_PLAN",
            },
        ]
    }
    water_selected = water_transition_rows(water_sample)
    assert len(water_selected) == 2
    assert any(row["katman"] == "KIYI/SU · ARKA PLAN" for row in water_selected)
    assert all(row["durum"] == "ALARM DEĞİL" for row in water_selected)


_self_check()

st.set_page_config(page_title="Gülbahçe Güncel Kör Alanlar", page_icon="☁️", layout="wide")
st.title("☁️ Gülbahçe Güncel Kör Alanlar")
st.caption(
    "En yeni Sentinel sahnesinde gerçekten görülemeyen, tarihsel açık sahnelerde kara olduğu "
    "kanıtlanmış Gülbahçe kümelerini; su/kıyı belirsizliklerinden ayrı gösterir."
)
st.info(
    "Bu ekran alarm veya saha görevi üretmez. Turuncu alanlar 250–6500 m² güncel kalite "
    "körlüğüdür; yalnız kör-alan devriyesini doğru yere yöneltmek için görünür tutulur. "
    "Mavi tonları tarihsel kara → güncel SCL=su geçişlerini iç-kara belirsizliği, kıyı ve "
    "geniş yüzey arka planı olarak ayrı gösterir. Ana Sentinel eşiği 250 m², MİKRO "
    "diagnostik bandı 150–249 m² olarak korunur."
)

current = load_json("gulbahce_latest_state_blind_review.json")
water = load_json("gulbahce_land_water_transition_watchlist.json")

blind_rows = current_blind_rows(current)
water_rows = water_transition_rows(water)

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Son Sentinel", current.get("kaynak_son_tarih", "Veri yok"))
m2.metric("Güncel kör 250–6500", len(blind_rows))
m3.metric("Devriyenin kapsadığı", sum(row["durum"] == "DEVRIYE_KAPSIYOR" for row in blind_rows))
m4.metric("Güncel kör MİKRO 150–249", int(current.get("guncel_sahne_mikro_kor_150_249", 0) or 0))
m5.metric("SCL-su geçiş izi", len(water_rows))

st.caption(
    "SCL-su geçiş hafızası: "
    f"iç-kara belirsiz {int(water.get('guncel_belirsiz_iz', 0) or 0)} · "
    f"kıyı arka plan {int(water.get('guncel_kiyi_arka_plan', 0) or 0)} · "
    f"geniş yüzey arka plan {int(water.get('guncel_genis_su_arka_plan', 0) or 0)}."
)

if current.get("durum") == "ok" and current.get("ayni_sentinel_sahnesi") is True:
    st.success(
        "Gülbahçe güncel-körlük verisi aynı Sentinel sahnesiyle tutarlı. "
        f"Kaynak: {current.get('kaynak_son_item', 'Sentinel öğesi belirtilmedi')}."
    )
elif current:
    st.warning("Gülbahçe güncel-körlük kaynağı tutarlı görünmüyor; bu ekran karar üretmez.")
else:
    st.warning("Gülbahçe güncel-körlük dosyası okunamadı.")

uncovered = [row for row in blind_rows if row["durum"] != "DEVRIYE_KAPSIYOR"]
if uncovered:
    smallest = min(uncovered, key=lambda row: (row["alan_m2"], row["enlem"], row["boylam"]))
    st.warning(
        f"{len(uncovered)} güncel kör küme mevcut devriye temsilinin dışında. "
        f"En küçük açık kör küme yaklaşık {smallest['alan_m2']} m²; bu bir şantiye alarmı değildir."
    )

show_water = st.toggle(
    "SCL-su / kıyı / geniş yüzey arka plan katmanlarını göster",
    value=True,
    help=(
        "Bu katman tarihsel kara olup son SCL sınıflamasında su görünen izleri gösterir. "
        "İç-kara belirsizliği, kıyı ve geniş/homojen yüzey izleri ayrı tutulur; hiçbiri "
        "şantiye alarmına veya saha görevine çevrilmez."
    ),
)

rows = list(blind_rows)
if show_water:
    rows.extend(water_rows)

signals = pd.DataFrame(rows)
if signals.empty:
    st.info("Seçili katmanlarda haritada gösterilecek güncel Gülbahçe kör alanı yok.")
else:
    st.pydeck_chart(
        pdk.Deck(
            map_provider="carto",
            map_style=SATELLITE_STYLE,
            layers=[
                pdk.Layer(
                    "ScatterplotLayer",
                    signals,
                    id="gulbahce-current-blind",
                    get_position="[boylam,enlem]",
                    get_fill_color="renk",
                    get_line_color=[255, 255, 255, 255],
                    get_radius="yaricap",
                    radius_min_pixels=11,
                    radius_max_pixels=28,
                    line_width_min_pixels=3,
                    stroked=True,
                    pickable=True,
                )
            ],
            initial_view_state=pdk.ViewState(latitude=38.336, longitude=26.646, zoom=13.2),
            tooltip={
                "html": (
                    "<b>{katman}</b><br>yaklaşık {alan_m2} m² · {durum}"
                    "<br>{neden}<br>{kanıt}"
                    "<br><small>Koordinat küme merkezidir; kesin adres/parsel değildir.</small>"
                )
            },
        ),
        use_container_width=True,
    )
    st.caption(
        "Turuncu = en yeni Sentinel sahnesinde kalite/bulut nedeniyle görülemeyen, tarihsel kara "
        "kanıtlı 250–6500 m² küme · Mavi tonları = tarihsel kara → güncel SCL=su geçişleri; "
        "iç-kara belirsizliği ile kıyı/geniş yüzey arka planı ayrıdır. Hiçbiri tek başına "
        "inşaat/kazı kabulü değildir."
    )
    st.dataframe(
        signals[
            ["katman", "durum", "alan_m2", "enlem", "boylam", "neden", "kanıt", "devriye_mesafe_m", "harita", "parsel_sorgu"]
        ].rename(
            columns={
                "katman": "Katman",
                "durum": "Durum",
                "alan_m2": "Alan (m²)",
                "enlem": "Enlem",
                "boylam": "Boylam",
                "neden": "Neden",
                "kanıt": "Kanıt",
                "devriye_mesafe_m": "Seçili devriyeye mesafe (m)",
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
    st.write("Güncel körlük Sentinel tarihi:", current.get("kaynak_son_tarih", "Veri yok"))
    st.write("Güncel körlük Sentinel öğesi:", current.get("kaynak_son_item", "Veri yok"))
    st.write("Tarihsel kara referans sahnesi:", current.get("kara_referans_sahne_sayisi", 0))
    st.write("Su/kıyı izleme son tarihi:", water.get("son_degerlendirilen_sentinel_tarihi", "Veri yok"))
    st.caption(
        "Ada/parsel, adres ve hukuki statü otomatik türetilmez. TKGM bağlantısı yalnız radar "
        "koordinatını manuel Parsel Sorgu ekranında açar. Kör alanın merkezi gerçek bir şantiye "
        "koordinatı değildir; yalnız gözlem boşluğunu temsil eder."
    )
