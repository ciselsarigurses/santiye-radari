import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote_plus

import pandas as pd
import pydeck as pdk
import streamlit as st
from bs4 import BeautifulSoup

from scanner import DB, INSTAGRAM_SEARCH_LINKS, connect, ensure_schema, scan_and_store


st.set_page_config(page_title="Şantiye Radarı", page_icon="📍", layout="wide")
ensure_schema()

SATELLITE_TILES = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
SATELLITE_LABELS = (
    "https://services.arcgisonline.com/ArcGIS/rest/services/Reference/"
    "World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"
)
SATELLITE_STYLE = {
    "version": 8,
    "sources": {
        "esri-imagery": {
            "type": "raster",
            "tiles": [SATELLITE_TILES],
            "tileSize": 256,
            "attribution": "Tiles © Esri",
        },
        "esri-labels": {
            "type": "raster",
            "tiles": [SATELLITE_LABELS],
            "tileSize": 256,
            "attribution": "Esri, OpenStreetMap contributors",
        },
    },
    "layers": [
        {
            "id": "esri-imagery",
            "type": "raster",
            "source": "esri-imagery",
            "minzoom": 0,
            "maxzoom": 19,
        },
        {
            "id": "esri-labels",
            "type": "raster",
            "source": "esri-labels",
            "minzoom": 0,
            "maxzoom": 19,
        },
    ],
}

# Kesin koordinatı olmayan kayıtları yanlış bir parsele yerleştirmek yerine
# mahalle merkezinde, adet belirten mavi bir küme olarak gösteriyoruz.
NEIGHBORHOOD_CENTERS = {
    "alaçatı": (38.2847573, 26.3745176),
    "ilıca": (38.3083827, 26.3607464),
    "reisdere": (38.3157625, 26.4173433),
    "uzunkuyu": (38.2842936, 26.5509824),
}

st.markdown(
    """
    <style>
    .block-container {padding-top: 1.5rem; padding-bottom: 3rem;}
    [data-testid="stMetric"] {background:#f7f7f8;border:1px solid #e5e5e5;
        padding:14px;border-radius:12px;}
    .radar-note {padding:12px 14px;border-radius:10px;background:#fff8e8;
        border-left:4px solid #f39c12;margin-bottom:14px;}
    </style>
    """,
    unsafe_allow_html=True,
)


def read_df(query, params=()):
    with connect() as connection:
        return pd.read_sql_query(query, connection, params=params)


def value(item, fallback="-"):
    if item is None or (isinstance(item, float) and pd.isna(item)):
        return fallback
    text = str(item).strip()
    return text if text and text.lower() != "nan" else fallback


def number(item, fallback=0):
    try:
        return fallback if pd.isna(item) else int(item)
    except (TypeError, ValueError):
        return fallback


def plain_text(item, fallback="-"):
    text = value(item, fallback)
    if text == fallback:
        return fallback
    return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)


def short_text(item, limit=110):
    text = plain_text(item, "Başlıksız bulgu")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def add_to_field(candidate):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with connect() as connection:
        exists = connection.execute(
            "SELECT id FROM santiyeler WHERE kaynak_url=? AND aktif=1 LIMIT 1",
            (candidate.kaynak_url,),
        ).fetchone()
        if exists:
            return False
        connection.execute(
            """INSERT INTO santiyeler
            (durum,mahalle,firma,proje,neden,internet_bilgisi,kaynak_url,
            son_kontrol,aktif) VALUES(?,?,?,?,?,?,?,?,1)""",
            (
                value(candidate.durum, "TURUNCU"), value(candidate.bolge, ""),
                value(candidate.firma, ""), value(candidate.proje, ""),
                value(candidate.sinyal, ""), value(candidate.notlar, ""),
                value(candidate.kaynak_url, ""), now,
            ),
        )
        connection.execute(
            "UPDATE internet_adaylari SET aktif=0 WHERE id=?", (int(candidate.id),)
        )
    return True


def archive_candidate(candidate_id):
    with connect() as connection:
        connection.execute(
            "UPDATE internet_adaylari SET aktif=0 WHERE id=?", (int(candidate_id),)
        )


st.title("📍 Şantiye Radarı")
st.caption("Çeşme + Uzunkuyu · Saha, internet, belediye ve indekslenmiş Instagram sinyalleri")

try:
    last_scan = read_df(
        "SELECT * FROM tarama_gecmisi ORDER BY id DESC LIMIT 1"
    )
except sqlite3.Error:
    last_scan = pd.DataFrame()

with st.sidebar:
    st.header("Radar Kontrolü")
    if not last_scan.empty:
        last = last_scan.iloc[0]
        st.caption(f"Son tarama: {value(last.get('bitis'))}")
        st.write(
            f"**{number(last.get('yeni'))} yeni** · "
            f"{number(last.get('guncellenen'))} güncellendi"
        )
    else:
        st.caption("Henüz otomatik tarama kaydı yok.")

    run_scan = st.button("🔎 Şimdi İnterneti Tara", type="primary", use_container_width=True)
    st.caption("Tarama yaklaşık 1–2 dakika sürebilir.")
    st.divider()
    st.markdown("**Kapsam**")
    st.write("• Genel web ve haber sonuçları")
    st.write("• Çeşme Belediyesi indeksleri")
    st.write("• Herkese açık, indekslenmiş Instagram sonuçları")
    st.caption("Kapalı hesaplar ve Instagram'ın arama motorlarından gizlediği içerikler görülemez.")

if run_scan:
    progress = st.progress(0, text="Tarama hazırlanıyor…")

    def show_progress(ratio, label):
        progress.progress(min(float(ratio), 1.0), text=f"Taranıyor: {label}")

    try:
        result = scan_and_store(show_progress)
        progress.progress(1.0, text="Tarama tamamlandı")
        if result["new"]:
            st.success(
                f"Radar {result['found']} uygun sonuç yakaladı; "
                f"{result['new']} tanesi yeni."
            )
        else:
            st.info(
                f"Tarama tamamlandı. {result['found']} uygun sonuç kontrol edildi; "
                "yeni kayıt bulunmadı."
            )
        if result["errors"]:
            st.warning(
                f"{len(result['errors'])} arama yanıt vermedi; çalışan kaynakların sonuçları kaydedildi."
            )
    except Exception as exc:
        st.error(f"Tarama tamamlanamadı: {type(exc).__name__}")


field_tab, web_tab, scan_tab, add_tab = st.tabs(
    ["🎯 Saha Listesi", "🌐 Radar Bulguları", "🔍 Tarama Merkezi", "➕ Yeni Kayıt"]
)

with field_tab:
    field = read_df(
        """SELECT id,durum,mahalle,ada,parsel,adres,enlem,boylam,
        firma,proje,neden,belediye_bilgisi,internet_bilgisi,harita_bilgisi,
        kaynak_url,son_kontrol FROM santiyeler WHERE aktif=1
        ORDER BY CASE durum WHEN 'KIRMIZI' THEN 1 ELSE 2 END, son_kontrol DESC"""
    )

    if field.empty:
        st.info("Henüz aktif saha kaydı yok. Radar bulgularından bir adayı saha listesine aktarabilirsin.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("🔴 Gidilmeli", int((field.durum == "KIRMIZI").sum()))
        c2.metric("🟠 Kontrol", int((field.durum == "TURUNCU").sum()))
        c3.metric("📍 Konum eksik", int((field.enlem.isna() | field.boylam.isna()).sum()))
        c4.metric("Toplam aktif", len(field))

        mapped = field.dropna(subset=["enlem", "boylam"]).copy()
        if not mapped.empty:
            mapped["enlem"] = pd.to_numeric(mapped.enlem, errors="coerce")
            mapped["boylam"] = pd.to_numeric(mapped.boylam, errors="coerce")
            mapped = mapped.dropna(subset=["enlem", "boylam"])
            mapped["renk"] = mapped.durum.map(
                {"KIRMIZI": [235, 45, 65, 235], "TURUNCU": [255, 145, 25, 235]}
            ).apply(lambda x: x if isinstance(x, list) else [60, 120, 180, 235])
            mapped["etiket"] = mapped.apply(
                lambda row: f"{value(row.mahalle, '')} | {value(row.ada)} / {value(row.parsel)}",
                axis=1,
            )

        missing = field[field.enlem.isna() | field.boylam.isna()].copy()
        missing["mahalle_anahtari"] = (
            missing.mahalle.fillna("").astype(str).str.strip().str.casefold()
        )
        approximate_rows = []
        for mahalle_key, group in missing.groupby("mahalle_anahtari"):
            center = NEIGHBORHOOD_CENTERS.get(mahalle_key)
            if not center:
                continue
            count = len(group)
            approximate_rows.append(
                {
                    "enlem": center[0],
                    "boylam": center[1],
                    "sayi": str(count),
                    "etiket": f"{value(group.iloc[0].mahalle)} • {count} kayıt",
                    "durum": "Yaklaşık bölge",
                    "neden": (
                        "Kesin ada/parsel koordinatı henüz yok; işaret mahalle "
                        "merkezini ve kayıt sayısını gösterir."
                    ),
                }
            )
        approximate = pd.DataFrame(approximate_rows)

        map_layers = []
        if not approximate.empty:
            map_layers.extend(
                [
                    pdk.Layer(
                        "ScatterplotLayer", approximate,
                        get_position="[boylam,enlem]",
                        get_fill_color=[35, 135, 255, 205],
                        get_line_color=[255, 255, 255, 255],
                        get_radius=230,
                        radius_min_pixels=15,
                        radius_max_pixels=28,
                        line_width_min_pixels=3,
                        stroked=True,
                        pickable=True,
                    ),
                    pdk.Layer(
                        "TextLayer", approximate,
                        get_position="[boylam,enlem]",
                        get_text="sayi",
                        get_color=[255, 255, 255, 255],
                        get_size=16,
                        size_min_pixels=13,
                        size_max_pixels=18,
                        get_text_anchor="'middle'",
                        get_alignment_baseline="'center'",
                    ),
                ]
            )
        if not mapped.empty:
            map_layers.append(
                pdk.Layer(
                    "ScatterplotLayer", mapped,
                    get_position="[boylam,enlem]",
                    get_fill_color="renk",
                    get_line_color=[255, 255, 255, 255],
                    get_radius=120,
                    radius_min_pixels=11,
                    radius_max_pixels=25,
                    line_width_min_pixels=3,
                    stroked=True,
                    pickable=True,
                )
            )

        st.pydeck_chart(
            pdk.Deck(
                map_provider="maplibre",
                map_style=SATELLITE_STYLE,
                layers=map_layers,
                initial_view_state=pdk.ViewState(
                    latitude=38.305, longitude=26.405, zoom=10.35
                ),
                tooltip={
                    "html": "<b>{etiket}</b><br>{durum}<br><small>{neden}</small>"
                },
            ),
            use_container_width=True,
        )
        st.caption(
            "🔴 Gidilmeli · 🟠 Kontrol · 🔵 Sayılı kümeler kesin koordinatı eksik "
            "kayıtların mahalle merkezidir (parsel konumu değildir). "
            "Uydu: Esri, yer adları ve mahalle merkezleri: OpenStreetMap katkıcıları."
        )

        export_columns = [
            "durum", "mahalle", "ada", "parsel", "adres", "firma", "proje",
            "neden", "kaynak_url", "son_kontrol",
        ]
        st.download_button(
            "📥 Saha listesini CSV indir",
            field[export_columns].to_csv(index=False).encode("utf-8-sig"),
            file_name="santiye_saha_listesi.csv",
            mime="text/csv",
        )

        st.subheader("Aktif noktalar")
        for _, row in field.iterrows():
            icon = "🔴" if row.durum == "KIRMIZI" else "🟠"
            label = f"{icon} {value(row.mahalle, 'Konum araştırılıyor')}"
            if value(row.ada) != "-" or value(row.parsel) != "-":
                label += f" — {value(row.ada)} / {value(row.parsel)}"
            with st.expander(label):
                st.markdown(
                    f"### {'MUTLAKA GİDİLMELİ' if row.durum == 'KIRMIZI' else 'KONTROL EDİLMELİ'}"
                )
                if value(row.firma) != "-":
                    st.write("**Firma:**", value(row.firma))
                if value(row.proje) != "-":
                    st.write("**Proje:**", value(row.proje))
                st.write("**Ada / Parsel:**", f"{value(row.ada)} / {value(row.parsel)}")
                st.write("**Adres:**", value(row.adres, "Henüz bulunamadı"))
                st.write("**Neden:**", value(row.neden))
                st.write("**Belediye:**", value(row.belediye_bilgisi, "Bilgi bulunmadı"))
                st.write("**İnternet:**", value(row.internet_bilgisi, "Bilgi bulunmadı"))
                st.write("**Harita kontrolü:**", value(row.harita_bilgisi, "Google Maps üzerinden kontrol edilebilir"))

                b1, b2 = st.columns(2)
                if pd.notna(row.enlem) and pd.notna(row.boylam):
                    b1.link_button(
                        "📍 Yol Tarifi",
                        f"https://www.google.com/maps/dir/?api=1&destination={row.enlem},{row.boylam}",
                        use_container_width=True,
                    )
                    b2.link_button(
                        "🛰️ Haritada Gör",
                        f"https://www.google.com/maps/search/?api=1&query={row.enlem},{row.boylam}",
                        use_container_width=True,
                    )
                elif value(row.adres) != "-":
                    b1.link_button(
                        "📍 Adresi Haritada Ara",
                        "https://www.google.com/maps/search/?api=1&query="
                        + quote_plus(value(row.adres) + " Çeşme İzmir"),
                        use_container_width=True,
                    )
                elif value(row.ada) != "-" and value(row.parsel) != "-":
                    b1.link_button(
                        "📍 Ada / Parseli Ara",
                        "https://www.google.com/maps/search/?api=1&query="
                        + quote_plus(
                            f"{value(row.mahalle, '')} {value(row.ada)} ada "
                            f"{value(row.parsel)} parsel Çeşme İzmir"
                        ),
                        use_container_width=True,
                    )
                if value(row.kaynak_url) != "-":
                    st.link_button("Kaynağı Aç", value(row.kaynak_url), use_container_width=True)
                st.caption(f"Son kontrol: {value(row.son_kontrol)}")

with web_tab:
    candidates = read_df(
        """SELECT id,firma,proje,bolge,sinyal,notlar,kaynak_url,
        ilk_gorulme,son_gorulme,kaynak_tipi,skor,durum
        FROM internet_adaylari WHERE aktif=1
        ORDER BY skor DESC, ilk_gorulme DESC"""
    )

    if candidates.empty:
        st.info("Henüz radar bulgusu yok. Sol menüden ‘Şimdi İnterneti Tara’ düğmesine basabilirsin.")
    else:
        m1, m2, m3 = st.columns(3)
        m1.metric("Otomatik bulgu", int(candidates.kaynak_tipi.notna().sum()))
        m2.metric("🔴 Güçlü sinyal", int((candidates.durum == "KIRMIZI").sum()))
        m3.metric("Instagram sinyali", int(candidates.kaynak_tipi.str.contains("Instagram", na=False).sum()))

        f1, f2, f3 = st.columns(3)
        regions = ["Tümü"] + sorted(candidates.bolge.dropna().unique().tolist())
        sources = ["Tümü"] + sorted(candidates.kaynak_tipi.dropna().unique().tolist())
        region_filter = f1.selectbox("Bölge", regions)
        source_filter = f2.selectbox("Kaynak", sources)
        min_score = f3.slider("En düşük sinyal puanı", 0, 10, 5)

        shown = candidates[candidates.skor.fillna(0) >= min_score]
        if region_filter != "Tümü":
            shown = shown[shown.bolge == region_filter]
        if source_filter != "Tümü":
            shown = shown[shown.kaynak_tipi == source_filter]

        st.caption(f"{len(shown)} bulgu gösteriliyor. Puan yükseldikçe sahaya gitme ihtiyacı güçlenir.")
        st.download_button(
            "📥 Radar bulgularını CSV indir",
            shown.to_csv(index=False).encode("utf-8-sig"),
            file_name="santiye_radar_bulgulari.csv",
            mime="text/csv",
        )

        for _, row in shown.iterrows():
            icon = "🔴" if value(row.durum) == "KIRMIZI" else "🟠"
            with st.expander(
                f"{icon} {value(row.bolge, 'Bölge belirsiz')} · {short_text(row.proje)}"
            ):
                st.write("**Kaynak:**", value(row.kaynak_tipi))
                st.write("**Sinyal:**", value(row.sinyal))
                st.write("**Radar puanı:**", f"{number(row.skor)}/10")
                st.write("**Bulunan bilgi:**", plain_text(row.notlar))
                st.caption(
                    f"İlk görülme: {value(row.ilk_gorulme)} · Son görülme: {value(row.son_gorulme)}"
                )
                if value(row.kaynak_url) != "-":
                    st.link_button("🔗 Kaynağı Aç", value(row.kaynak_url), use_container_width=True)
                a1, a2 = st.columns(2)
                if a1.button("🎯 Saha listesine aktar", key=f"field_{int(row.id)}", use_container_width=True):
                    if add_to_field(row):
                        st.success("Saha listesine aktarıldı.")
                    else:
                        st.info("Bu kaynak zaten saha listesinde.")
                    st.rerun()
                if a2.button("Arşivle", key=f"archive_{int(row.id)}", use_container_width=True):
                    archive_candidate(row.id)
                    st.rerun()

with scan_tab:
    st.subheader("Radar nasıl çalışıyor?")
    st.markdown(
        """
        1. Çeşme, Alaçatı, Ilıca, Reisdere, Ovacık, Dalyan, Çiftlikköy,
           Musalla ve Uzunkuyu için hedefli aramalar yapar.
        2. “Ruhsat”, “temel”, “hafriyat”, “şantiye”, “yeni inşaat” ve
           “villa projesi” gibi satış fırsatı sinyallerini puanlar.
        3. Aynı bağlantıyı tekrar bulursa yeni kayıt açmaz; mevcut kaydın
           son görülme tarihini günceller.
        4. Güçlü sinyalleri kırmızı, teyit edilmesi gerekenleri turuncu gösterir.
        """
    )
    st.markdown(
        '<div class="radar-note"><b>Net sınır:</b> Instagram doğrudan ve eksiksiz '
        'taranamaz. Radar, yalnızca herkese açık olup Google/DuckDuckGo tarafından '
        'indekslenmiş Instagram sayfalarını yakalar. Aşağıdaki bağlantılar manuel '
        'kontrol için hazırdır.</div>',
        unsafe_allow_html=True,
    )
    for label, link in INSTAGRAM_SEARCH_LINKS:
        st.link_button(f"Instagram kontrolü · {label}", link, use_container_width=True)

    history = read_df(
        """SELECT bitis,bulunan,yeni,guncellenen,hata
        FROM tarama_gecmisi ORDER BY id DESC LIMIT 10"""
    )
    if not history.empty:
        st.subheader("Son taramalar")
        st.dataframe(
            history.rename(
                columns={
                    "bitis": "Tarih", "bulunan": "Uygun sonuç", "yeni": "Yeni",
                    "guncellenen": "Güncellenen", "hata": "Kaynak hataları",
                }
            ),
            hide_index=True,
            use_container_width=True,
        )

with add_tab:
    st.subheader("Elle saha kaydı ekle")
    with st.form("new_field"):
        a, b = st.columns(2)
        status = a.selectbox("Durum", ["TURUNCU", "KIRMIZI"])
        neighborhood = b.text_input("Mahalle")
        block = a.text_input("Ada")
        parcel = b.text_input("Parsel")
        address = st.text_input("Adres")
        company = a.text_input("Firma")
        project = b.text_input("Proje")
        reason = st.text_area("Neden / bulunan bilgi")
        source = st.text_input("Kaynak URL")
        lat = a.number_input("Enlem", value=None, format="%.7f")
        lon = b.number_input("Boylam", value=None, format="%.7f")
        submitted = st.form_submit_button("Kaydet", type="primary", use_container_width=True)

    if submitted:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with connect() as connection:
            connection.execute(
                """INSERT INTO santiyeler
                (durum,mahalle,ada,parsel,adres,enlem,boylam,firma,proje,
                neden,kaynak_url,son_kontrol,aktif)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (
                    status, neighborhood, block, parcel, address, lat, lon,
                    company, project, reason, source, now,
                ),
            )
        st.success("Kayıt saha listesine eklendi.")

st.caption("Şantiye Radarı karar destek aracıdır. Kırmızı işaret saha teyidi önerir; kesin yapı ruhsatı anlamına gelmez.")
