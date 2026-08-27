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

SATELLITE_STYLE = (
    "https://raw.githubusercontent.com/ciselsarigurses/santiye-radari/"
    "main/satellite-style.json"
)

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


def field_record_classification(row):
    municipal = value(row.get("belediye_bilgisi"), "").casefold()
    combined = " ".join(
        value(row.get(column), "")
        for column in (
            "neden", "belediye_bilgisi", "internet_bilgisi", "kaynak_url"
        )
    ).casefold()
    if "ruhsat doğrulandı" in municipal or "yapı ruhsat no" in municipal:
        return "Yapı ruhsatı", "Ruhsat doğrulandı"
    if "meclis" in combined or "imar planı" in combined:
        return "İmar/meclis sinyali", "Ruhsat teyit edilmedi"
    if "imardurumu" in combined or "e-imar" in combined:
        return "E-İmar parsel kaydı", "Ruhsat teyit edilmedi"
    if "ruhsat aldı" in combined or "yapı ruhsatı" in combined:
        return "Ruhsat internet sinyali", "Belediye ruhsatı teyitsiz"
    active_terms = (
        "yapım sürüyor", "inşaata başladı", "temel atıldı", "hafriyat",
        "şantiye kuruldu", "kaba inşaat",
    )
    if any(term in combined for term in active_terms):
        return "Aktif inşaat internet sinyali", "Belediye ruhsatı teyitsiz"
    return "Saha/internet kontrol adayı", "Ruhsat teyit edilmedi"


def selected_map_object(event, layer_id):
    try:
        objects = event.selection.objects
        selected = objects.get(layer_id, [])
        return selected[0] if selected else None
    except (AttributeError, KeyError, TypeError, IndexError):
        return None


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
        field = field.reset_index(drop=True)
        # Harita ve alt listedeki # numarası kalıcı veritabanı kayıt kimliğidir.
        # Tarama/öncelik sırası değişse bile aynı saha aynı numarada kalır.
        field["liste_no"] = field.id.astype(int)
        classifications = field.apply(
            field_record_classification, axis=1, result_type="expand"
        )
        classifications.columns = ["kayit_turu", "ruhsat_durumu"]
        field[["kayit_turu", "ruhsat_durumu"]] = classifications

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("🔴 Güçlü sinyal", int((field.durum == "KIRMIZI").sum()))
        c2.metric("🟠 Kontrol", int((field.durum == "TURUNCU").sum()))
        c3.metric("✅ Ruhsat teyitli", int(field.ruhsat_durumu.str.startswith("Ruhsat doğrulandı").sum()))
        c4.metric("📍 Konum eksik", int((field.enlem.isna() | field.boylam.isna()).sum()))
        c5.metric("Toplam aktif", len(field))
        st.warning(
            "Belediye meclisinde veya E-İmar'da görünen parsel, tek başına yapı "
            "ruhsatı ya da inşaat başlangıcı değildir. Ruhsat teyidi olmayan kayıtlar "
            "aşağıda açıkça işaretlenmiştir."
        )
        st.caption(
            "# numarası sabit kayıt numarasıdır; öncelik puanı değildir. "
            "Saha önceliğini kırmızı/turuncu renk gösterir."
        )

        mapped = field.dropna(subset=["enlem", "boylam"]).copy()
        if not mapped.empty:
            mapped["enlem"] = pd.to_numeric(mapped.enlem, errors="coerce")
            mapped["boylam"] = pd.to_numeric(mapped.boylam, errors="coerce")
            mapped = mapped.dropna(subset=["enlem", "boylam"])
            mapped["renk"] = mapped.durum.map(
                {"KIRMIZI": [235, 45, 65, 235], "TURUNCU": [255, 145, 25, 235]}
            ).apply(lambda x: x if isinstance(x, list) else [60, 120, 180, 235])
            mapped["sayi"] = mapped.liste_no.astype(str)
            mapped["etiket"] = mapped.apply(
                lambda row: (
                    f"#{int(row.liste_no)} • {value(row.mahalle, '')} | "
                    f"{value(row.ada)} / {value(row.parsel)}"
                ),
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
            group_status = "KIRMIZI" if (group.durum == "KIRMIZI").any() else "TURUNCU"
            group_color = (
                [235, 45, 65, 235] if group_status == "KIRMIZI"
                else [255, 145, 25, 235]
            )
            list_numbers = ", ".join(f"#{int(no)}" for no in group.liste_no)
            approximate_rows.append(
                {
                    "enlem": center[0],
                    "boylam": center[1],
                    "sayi": str(count),
                    "mahalle": value(group.iloc[0].mahalle),
                    "etiket": f"{value(group.iloc[0].mahalle)} • {count} kayıt",
                    "durum": f"{group_status} • yaklaşık mahalle merkezi",
                    "renk": group_color,
                    "kayit_turu": "Koordinatı eksik kayıt kümesi",
                    "kayit_idleri": ",".join(str(int(item)) for item in group.id),
                    "kayit_nolari": list_numbers,
                    "neden": (
                        f"Bu sayı sıra numarası değil; {count} koordinatsız kaydın "
                        "mahalle toplamıdır. Kesin parsel konumu değildir."
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
                        id="area-groups",
                        get_position="[boylam,enlem]",
                        get_fill_color="renk",
                        get_line_color=[35, 135, 255, 255],
                        get_radius=230,
                        radius_min_pixels=15,
                        radius_max_pixels=28,
                        line_width_min_pixels=5,
                        stroked=True,
                        pickable=True,
                    ),
                    pdk.Layer(
                        "TextLayer", approximate,
                        id="area-group-labels",
                        get_position="[boylam,enlem]",
                        get_text="sayi",
                        get_color=[255, 255, 255, 255],
                        get_size=16,
                        size_min_pixels=13,
                        size_max_pixels=18,
                        get_text_anchor="'middle'",
                        get_alignment_baseline="'center'",
                        pickable=False,
                    ),
                ]
            )
        if not mapped.empty:
            map_layers.extend(
                [
                    pdk.Layer(
                        "ScatterplotLayer", mapped,
                        id="site-points",
                        get_position="[boylam,enlem]",
                        get_fill_color="renk",
                        get_line_color=[255, 255, 255, 255],
                        get_radius=120,
                        radius_min_pixels=12,
                        radius_max_pixels=26,
                        line_width_min_pixels=4,
                        stroked=True,
                        pickable=True,
                    ),
                    pdk.Layer(
                        "TextLayer", mapped,
                        id="site-labels",
                        get_position="[boylam,enlem]",
                        get_text="sayi",
                        get_color=[255, 255, 255, 255],
                        get_size=15,
                        size_min_pixels=12,
                        size_max_pixels=17,
                        get_text_anchor="'middle'",
                        get_alignment_baseline="'center'",
                        pickable=False,
                    ),
                ]
            )

        map_event = st.pydeck_chart(
            pdk.Deck(
                map_provider="carto",
                map_style=SATELLITE_STYLE,
                layers=map_layers,
                initial_view_state=pdk.ViewState(
                    latitude=38.305, longitude=26.405, zoom=10.35
                ),
                tooltip={
                    "html": (
                        "<b>{etiket}</b><br>{durum}<br>{kayit_turu}"
                        "<br><small>{neden}</small>"
                    )
                },
            ),
            use_container_width=True,
            key="field_map",
            on_select="rerun",
            selection_mode="single-object",
        )
        st.caption(
            "🔴 Güçlü sinyal · 🟠 Kontrol · Beyaz çerçeveli tek sayı = listede aynı "
            "numaralı kesin nokta · Mavi çerçeveli sayı = o mahalledeki koordinatsız "
            "kayıt adedi (sıra numarası değildir). Bir işarete tıklayarak detayını aç. "
            "Uydu: Esri, yer adları ve mahalle merkezleri: OpenStreetMap katkıcıları."
        )

        selected_site = selected_map_object(map_event, "site-points")
        selected_area = selected_map_object(map_event, "area-groups")
        if selected_site:
            try:
                selected_id = int(float(selected_site.get("id")))
            except (TypeError, ValueError):
                selected_id = -1
            selected_rows = field[field.id == selected_id]
            if not selected_rows.empty:
                selected_row = selected_rows.iloc[0]
                with st.container(border=True):
                    icon = "🔴" if selected_row.durum == "KIRMIZI" else "🟠"
                    st.markdown(
                        f"#### {icon} #{int(selected_row.liste_no)} • "
                        f"{value(selected_row.mahalle, 'Konum araştırılıyor')}"
                    )
                    st.write("**Kayıt türü:**", selected_row.kayit_turu)
                    st.write("**Ruhsat:**", selected_row.ruhsat_durumu)
                    if value(selected_row.firma) != "-":
                        st.write("**Firma:**", value(selected_row.firma))
                    if value(selected_row.proje) != "-":
                        st.write("**Proje:**", value(selected_row.proje))
                    st.write(
                        "**Ada / Parsel:**",
                        f"{value(selected_row.ada)} / {value(selected_row.parsel)}",
                    )
                    st.write("**Ön bilgi:**", value(selected_row.neden))
                    if value(selected_row.kaynak_url) != "-":
                        st.link_button("Kaynağı Aç", value(selected_row.kaynak_url))
        elif selected_area:
            selected_ids = [
                int(item) for item in str(selected_area.get("kayit_idleri", "")).split(",")
                if item.strip().isdigit()
            ]
            area_records = field[field.id.isin(selected_ids)].copy()
            if not area_records.empty:
                st.markdown(
                    f"#### 🟠 {value(selected_area.get('mahalle'))} • "
                    f"{len(area_records)} koordinatsız kayıt"
                )
                st.info(
                    "Haritadaki sayı kayıt adedidir. Aşağıdaki # numaralar, "
                    "‘Aktif noktalar’ listesindeki kayıtlarla aynıdır."
                )
                st.dataframe(
                    area_records[[
                        "liste_no", "durum", "ada", "parsel", "kayit_turu",
                        "ruhsat_durumu",
                    ]].rename(columns={
                        "liste_no": "#", "durum": "Durum", "ada": "Ada",
                        "parsel": "Parsel", "kayit_turu": "Kayıt türü",
                        "ruhsat_durumu": "Ruhsat",
                    }),
                    hide_index=True,
                    use_container_width=True,
                )
        else:
            st.info("Haritadaki kırmızı/turuncu işarete tıkla; ön bilgi burada açılacak.")

        if not approximate.empty:
            st.markdown("**Mavi çerçeveli sayıların açıklaması**")
            st.dataframe(
                approximate[["mahalle", "sayi", "kayit_nolari"]].rename(
                    columns={
                        "mahalle": "Mahalle", "sayi": "Haritadaki sayı (adet)",
                        "kayit_nolari": "Alttaki kayıt numaraları",
                    }
                ),
                hide_index=True,
                use_container_width=True,
            )

        export_columns = [
            "liste_no", "durum", "kayit_turu", "ruhsat_durumu", "mahalle",
            "ada", "parsel", "adres", "firma", "proje", "neden",
            "kaynak_url", "son_kontrol",
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
            label = (
                f"#{int(row.liste_no)} {icon} "
                f"{value(row.mahalle, 'Konum araştırılıyor')}"
            )
            if value(row.ada) != "-" or value(row.parsel) != "-":
                label += f" — {value(row.ada)} / {value(row.parsel)}"
            with st.expander(label):
                st.markdown(
                    f"### {'MUTLAKA GİDİLMELİ' if row.durum == 'KIRMIZI' else 'KONTROL EDİLMELİ'}"
                )
                st.write("**Kayıt türü:**", row.kayit_turu)
                st.write("**Ruhsat durumu:**", row.ruhsat_durumu)
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
    st.markdown("#### Resmî ruhsat ve inşaata başlama verisi")
    st.warning(
        "Çeşme Belediyesi E-İmar ekranı parselin imar koşullarını gösterir; "
        "yapı ruhsatı verildiğini veya şantiyenin başladığını kanıtlamaz. E-Ruhsat "
        "sistemi giriş korumalıdır. Bu nedenle belediye belgesi/API/CSV kaydı "
        "gelmeden hiçbir parsel ‘ruhsat teyitli’ sayılmaz."
    )
    official_1, official_2 = st.columns(2)
    official_1.link_button(
        "Çeşme Belediyesi E-Belediye",
        "https://www.cesme.bel.tr/e-belediye",
        use_container_width=True,
    )
    official_2.link_button(
        "Çeşme Belediyesi E-Ruhsat",
        "https://keos.cesme.bel.tr/BELNET/",
        use_container_width=True,
    )
    st.caption(
        "Yetkili veri talebi: Ruhsat ve Denetim Müdürlüğü · "
        "ruhsat.denetim@cesme.bel.tr · 0 232 750 07 50 / 2801. "
        "Gerekli alanlar: ruhsat tarihi/numarası, mahalle, ada, parsel, yapı sahibi "
        "ve mümkünse işe başlama/temel-hafriyat tarihi."
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
        permit_verification = st.selectbox(
            "Yapı ruhsatı teyidi",
            ["Teyit edilmedi", "Belediye belgesiyle doğrulandı"],
            help="Yalnızca belediye E-Ruhsat çıktısı, resmî yazı veya yetkili veri kaydı varsa doğrulandı seç.",
        )
        permit_number = st.text_input("Yapı ruhsat numarası (varsa)")
        lat = a.number_input("Enlem", value=None, format="%.7f")
        lon = b.number_input("Boylam", value=None, format="%.7f")
        submitted = st.form_submit_button("Kaydet", type="primary", use_container_width=True)

    if submitted:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        municipal_info = ""
        if permit_verification == "Belediye belgesiyle doğrulandı":
            municipal_info = "Ruhsat doğrulandı"
            if permit_number.strip():
                municipal_info += f" • Yapı ruhsat no: {permit_number.strip()}"
        with connect() as connection:
            connection.execute(
                """INSERT INTO santiyeler
                (durum,mahalle,ada,parsel,adres,enlem,boylam,firma,proje,
                neden,belediye_bilgisi,kaynak_url,son_kontrol,aktif)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (
                    status, neighborhood, block, parcel, address, lat, lon,
                    company, project, reason, municipal_info, source, now,
                ),
            )
        st.success("Kayıt saha listesine eklendi.")

st.caption("Şantiye Radarı karar destek aracıdır. Kırmızı işaret saha teyidi önerir; kesin yapı ruhsatı anlamına gelmez.")
