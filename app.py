
import sqlite3
from pathlib import Path
from urllib.parse import quote_plus
import streamlit as st
import pandas as pd
import pydeck as pdk

DB=Path(__file__).with_name("santiye.db")
def conn(): return sqlite3.connect(DB)

st.set_page_config(page_title="Şantiye Radarı", page_icon="📍", layout="wide")
st.title("Şantiye Radarı")
st.caption("Çeşme + Uzunkuyu | İnternet + Belediye + Google Maps")

with conn() as c:
    df=pd.read_sql_query("""SELECT id,durum,mahalle,ada,parsel,adres,enlem,boylam,
        firma,proje,neden,belediye_bilgisi,internet_bilgisi,harita_bilgisi,
        kaynak_url,son_kontrol FROM santiyeler WHERE aktif=1
        ORDER BY CASE durum WHEN 'KIRMIZI' THEN 1 ELSE 2 END, son_kontrol DESC""",c)

if df.empty:
    st.info("Henüz aktif kayıt yok.")
else:
    c1,c2,c3=st.columns(3)
    c1.metric("🔴 Gidilmeli",int((df.durum=="KIRMIZI").sum()))
    c2.metric("🟠 Kontrol",int((df.durum=="TURUNCU").sum()))
    c3.metric("📍 Konum eksik",int((df.enlem.isna()|df.boylam.isna()).sum()))

    mapped=df.dropna(subset=["enlem","boylam"]).copy()
    if not mapped.empty:
        mapped["renk"]=mapped.durum.map({"KIRMIZI":[220,40,40],"TURUNCU":[240,140,30]})
        mapped["etiket"]=mapped.apply(lambda r:f"{r.mahalle or ''} | {r.ada or '-'} / {r.parsel or '-'}",axis=1)
        st.pydeck_chart(pdk.Deck(
            layers=[pdk.Layer("ScatterplotLayer",mapped,get_position="[boylam,enlem]",
                              get_fill_color="renk",get_radius=55,pickable=True)],
            initial_view_state=pdk.ViewState(latitude=38.31,longitude=26.30,zoom=10.2),
            tooltip={"html":"<b>{etiket}</b><br>{durum}<br>{neden}"}
        ))

    st.subheader("Aktif noktalar")
    for _,r in df.iterrows():
        icon="🔴" if r.durum=="KIRMIZI" else "🟠"
        label=f"{icon} {r.mahalle or 'Konum araştırılıyor'}"
        if r.ada or r.parsel: label+=f" — {r.ada or '-'} / {r.parsel or '-'}"
        with st.expander(label):
            st.markdown(f"### {'MUTLAKA GİDİLMELİ' if r.durum=='KIRMIZI' else 'KONTROL EDİLMELİ'}")
            if r.firma: st.write("**Firma:**",r.firma)
            if r.proje: st.write("**Proje:**",r.proje)
            st.write("**Ada / Parsel:**",f"{r.ada or '-'} / {r.parsel or '-'}")
            st.write("**Adres:**",r.adres or "Henüz bulunamadı")
            st.write("**Neden:**",r.neden or "-")
            st.write("**Belediye:**",r.belediye_bilgisi or "Bilgi bulunmadı")
            st.write("**İnternet:**",r.internet_bilgisi or "Bilgi bulunmadı")
            st.write("**Harita kontrolü:**",r.harita_bilgisi or "Google Maps üzerinden kontrol edilebilir")
            b1,b2=st.columns(2)
            if pd.notna(r.enlem) and pd.notna(r.boylam):
                b1.link_button("📍 Google Maps / Yol Tarifi",
                    f"https://www.google.com/maps/dir/?api=1&destination={r.enlem},{r.boylam}",
                    use_container_width=True)
                b2.link_button("🛰️ Google Maps'te Gör",
                    f"https://www.google.com/maps/search/?api=1&query={r.enlem},{r.boylam}",
                    use_container_width=True)
            elif r.adres:
                b1.link_button("📍 Adresi Google Maps'te Ara",
                    f"https://www.google.com/maps/search/?api=1&query={quote_plus(str(r.adres) + ' Çeşme İzmir')}",
                    use_container_width=True)
            if r.kaynak_url:
                st.link_button("Kaynağı Aç",r.kaynak_url,use_container_width=True)
            st.caption(f"Son kontrol: {r.son_kontrol or '-'}")

st.divider()
st.subheader("İnternetten bulunan proje adayları")
with conn() as c:
    try:
        webdf=pd.read_sql_query("""SELECT firma,proje,bolge,sinyal,notlar,kaynak_url
          FROM internet_adaylari WHERE aktif=1 ORDER BY ilk_gorulme DESC""",c)
        for _,r in webdf.iterrows():
            with st.expander(f"🟠 {r.proje or r.firma} — {r.bolge or ''}"):
                st.write("**Firma:**",r.firma or "-")
                st.write("**Bulgu:**",r.sinyal or "-")
                st.write("**Not:**",r.notlar or "-")
                if r.kaynak_url: st.link_button("Kaynağı Aç",r.kaynak_url)
    except Exception:
        pass

st.divider()
with st.expander("Yeni kayıt ekle"):
    with st.form("new"):
        a,b=st.columns(2)
        durum=a.selectbox("Durum",["TURUNCU","KIRMIZI"])
        mahalle=b.text_input("Mahalle")
        ada=a.text_input("Ada"); parsel=b.text_input("Parsel")
        adres=st.text_input("Adres")
        firma=a.text_input("Firma"); proje=b.text_input("Proje")
        neden=st.text_area("Neden / bulunan bilgi")
        kaynak=st.text_input("Kaynak URL")
        lat=a.number_input("Enlem",value=None,format="%.7f")
        lon=b.number_input("Boylam",value=None,format="%.7f")
        if st.form_submit_button("Kaydet",use_container_width=True):
            with conn() as c:
                c.execute("""INSERT INTO santiyeler
                (durum,mahalle,ada,parsel,adres,enlem,boylam,firma,proje,neden,kaynak_url,aktif)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,1)""",
                (durum,mahalle,ada,parsel,adres,lat,lon,firma,proje,neden,kaynak))
            st.success("Kaydedildi. Sayfayı yenileyin.")
