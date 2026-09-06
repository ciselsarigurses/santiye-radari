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
MAIN_EARLY_VISIBLE_MAX_AGE_DAYS = 2


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


def current_main_early_candidates(report):
    """Ana üretimde ERKEN seçilmiş taze 250–800 m² adayları görünür tutar.

    ``yeni_goruntu`` günlük taramanın o gün yeni Sentinel sahnesi görüp görmediğini
    anlatır; yeni sahneden sonraki gün False olması aday kanıtının bayatladığı anlamına
    gelmez. Bu yüzden ana raporun ölçtüğü uydu kanıt yaşı en fazla iki gün olduğu sürece
    mevcut ERKEN aday haritada kalır. Kanıt yaşı henüz yoksa yalnız gerçekten yeni
    görüntüyle gelen aday güvenli geri dönüş olarak kabul edilir. Bu katman yeni alarm
    veya saha görevi üretmez ve 250 m² ana eşiği değiştirmez.
    """
    selected = []
    for candidate in report.get("saha_adaylari", []):
        if not isinstance(candidate, dict):
            continue
        area = as_float(candidate.get("alan_m2"))
        if area is None or not (250 <= area <= 800):
            continue
        if str(candidate.get("oncelik") or "").strip().upper() != "ERKEN":
            continue

        evidence_age = as_float(candidate.get("uydu_kanit_yasi_gun"))
        if evidence_age is None:
            if candidate.get("yeni_goruntu") is not True:
                continue
        elif not (0 <= evidence_age <= MAIN_EARLY_VISIBLE_MAX_AGE_DAYS):
            continue

        try:
            lat = float(candidate.get("enlem"))
            lon = float(candidate.get("boylam"))
        except (TypeError, ValueError):
            continue
        item = dict(candidate)
        item["enlem"] = lat
        item["boylam"] = lon
        item["alan_m2"] = area
        selected.append(item)
    return selected


def _self_check_main_early_visibility():
    base = {
        "oncelik": "ERKEN",
        "alan_m2": 500,
        "enlem": 38.27,
        "boylam": 26.36,
        "yeni_goruntu": False,
    }
    fresh = {**base, "uydu_kanit_yasi_gun": 1}
    stale = {**base, "uydu_kanit_yasi_gun": 3}
    fallback_new = {**base, "yeni_goruntu": True}
    below_main_threshold = {**fresh, "alan_m2": 249}

    assert len(current_main_early_candidates({"saha_adaylari": [fresh]})) == 1
    assert current_main_early_candidates({"saha_adaylari": [stale]}) == []
    assert len(current_main_early_candidates({"saha_adaylari": [fallback_new]})) == 1
    assert current_main_early_candidates({"saha_adaylari": [below_main_threshold]}) == []


def current_edge_risk_candidates(review):
    """Ana Sentinel bileşeni analiz kutusu kenarına temas eden/yaklaşan adayları döndür.

    Kaynak dosya diagnostiktir; bu yardımcı işlev de alarm, görev veya koordinat
    değiştirmez. Yalnız alan/merkez koordinatının bbox kırpmasından etkilenme riskini
    ana zemin haritasında görünür kılar.
    """
    selected = []
    regions = review.get("bolgeler") or {}
    if not isinstance(regions, dict):
        return selected
    for region_key, region in regions.items():
        if not isinstance(region, dict):
            continue
        for candidate in region.get("adaylar") or []:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("kenara_temas") is not True and candidate.get("kenara_yakin") is not True:
                continue
            lat = as_float(candidate.get("enlem"))
            lon = as_float(candidate.get("boylam"))
            area = as_float(candidate.get("alan_m2"))
            if lat is None or lon is None or area is None:
                continue
            item = dict(candidate)
            item["region_key"] = region_key
            item["bolge"] = region.get("bolge", region_key)
            item["enlem"] = lat
            item["boylam"] = lon
            item["alan_m2"] = area
            selected.append(item)
    return selected


_self_check_main_early_visibility()

st.set_page_config(page_title="Erken Zemin Sinyalleri", page_icon="🧭", layout="wide")
st.title("🧭 Erken Zemin Sinyalleri")
st.caption(
    "Ana 250–800 m² uydu kanıtı en fazla 2 günlük Sentinel ERKEN adaylarını, güçlü "
    "temporal/lokal kanıtı, 150–249 m² MİKRO ŞANTİYE izlerini, Gülbahçe kalite-kör "
    "ceplerini ve analiz kenarına yaklaşan ana adayları tek zemin haritasında birlikte gösterir."
)
st.info(
    "Bu sayfa yeni alarm veya saha görevi üretmez. Kırmızı ana ERKEN noktalar, "
    "günlük raporda zaten 250 m² ana üretim eşiğiyle seçilmiş ve uydu kanıtı en fazla "
    "2 günlük mevcut görevlerdir. MİKRO ŞANTİYE katmanı 150–249 m² aralığında yalnız "
    "güçlü lokal/kompakt + temporal kanıtı olan izleri arka planda tutar. Turuncu "
    "Gülbahçe noktaları görüntü kalitesi körlüğünü; sarı noktalar ise mevcut ana adayın "
    "analiz bbox kenarına çok yakın olduğunu gösterir. Bu iki diagnostik katman da kendi "
    "başına şantiye alarmı değildir."
)

latest = load_json("latest_report.json")
temporal = load_json("temporal_local_watch.json")
micro = load_json("micro_site_watchlist.json")
gulbahce = load_json("gulbahce_coverage_guard.json")
gulbahce_blind = load_json("gulbahce_micro_blind_capacity.json")
edge_review = load_json("active_edge_candidate_review.json")
main_early = current_main_early_candidates(latest)
edge_risks = current_edge_risk_candidates(edge_review)
blind_examples = [
    item
    for item in (gulbahce_blind.get("kor_kume_ornekleri") or [])
    if isinstance(item, dict)
]

coverage_status = gulbahce.get("durum", "veri_yok")
coverage_pct = as_float(gulbahce.get("tampon_kapsama_yuzde"))
context_pct = as_float(gulbahce.get("baglam_kapsama_yuzde"))
edge_margin = as_float(gulbahce.get("baglam_kenar_marji_m"))

m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("Ana ERKEN 250–800 · ≤2 gün", len(main_early))
m2.metric("250–900 m² temporal-lokal", int(temporal.get("aday_sayisi", 0) or 0))
m3.metric("MİKRO güçlü güncel", int(micro.get("guncel_guclu", 0) or 0))
m4.metric("MİKRO arka plan", int(micro.get("arka_plan_takip", 0) or 0))
m5.metric(
    "Gülbahçe kapsama",
    f"%{coverage_pct:.1f}" if coverage_pct is not None else "Veri yok",
)
m6.metric("Kenar-yakın ana aday", len(edge_risks))

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

if gulbahce_blind.get("mikro_korluk_kapasitesi_var"):
    st.warning(
        "Gülbahçe 2 km içinde görüntü kalitesi nedeniyle gözlenemeyen ve 150–249 m² "
        "bir müdahaleyi kendi içinde gizleyebilecek kör cepler var. Bunlar alarm değildir; "
        "turuncu temsil noktaları yalnız körlüğün konumunu görünür tutar."
    )

if edge_risks:
    closest = min(
        edge_risks,
        key=lambda item: as_float(item.get("en_yakin_kenar_m"))
        if as_float(item.get("en_yakin_kenar_m")) is not None
        else float("inf"),
    )
    closest_m = as_float(closest.get("en_yakin_kenar_m"))
    st.warning(
        f"{len(edge_risks)} ana Sentinel adayı analiz kutusunun kenarına çok yakın. "
        + (
            f"En yakın kayıt yaklaşık {closest_m:.0f} m ile "
            f"{str(closest.get('en_yakin_kenar') or 'bir')} kenarında. "
            if closest_m is not None
            else ""
        )
        + "Bu, adayın şantiye olasılığını yükseltmez; yalnız ölçülen alan veya temsil "
        "koordinatının sınır kırpmasından etkilenme riskini gösterir."
    )

rows = []
for candidate in main_early:
    lat = candidate["enlem"]
    lon = candidate["boylam"]
    area = candidate["alan_m2"]
    rows.append(
        {
            "katman": "ANA ERKEN 250–800",
            "durum": candidate.get("saha_durumu", candidate.get("oncelik", "ERKEN")),
            "bolge": candidate.get("bolge", "-"),
            "mevki": candidate.get("mahalle", "Mevki doğrulanmadı"),
            "alan_m2": int(round(area)),
            "enlem": lat,
            "boylam": lon,
            "kanıt": (
                f"{candidate.get('sinyal', 'Güçlü küçük-saha Sentinel değişimi')} · "
                f"{candidate.get('onceki_tarih', '-')} → {candidate.get('son_tarih', '-')}"
            ),
            "son_sahne": candidate.get("son_tarih", "-"),
            "renk": [230, 65, 55, 235],
            "yaricap": 185,
            "harita": map_link(lat, lon),
            "parsel_sorgu": parcel_link(lat, lon),
        }
    )

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

for blind in blind_examples:
    lat = as_float(blind.get("enlem"))
    lon = as_float(blind.get("boylam"))
    area = as_float(blind.get("alan_m2"))
    if lat is None or lon is None or area is None:
        continue
    rows.append(
        {
            "katman": "GÜLBAHÇE KÖR CEP",
            "durum": "ALARM DEĞİL · MİKRO İZ GİZLEYEBİLİR",
            "bolge": "Gülbahçe 2 km kalite-körlük diagnostik alanı",
            "mevki": "Gülbahçe kör cep merkezi",
            "alan_m2": int(round(area)),
            "enlem": lat,
            "boylam": lon,
            "kanıt": str(blind.get("neden") or "Sentinel kalite körlüğü"),
            "son_sahne": gulbahce_blind.get("kaynak_son_tarih", "-"),
            "renk": [235, 145, 40, 175],
            "yaricap": 145,
            "harita": map_link(lat, lon),
            "parsel_sorgu": parcel_link(lat, lon),
        }
    )

for candidate in edge_risks:
    lat = candidate["enlem"]
    lon = candidate["boylam"]
    area = candidate["alan_m2"]
    touches = candidate.get("kenara_temas") is True
    nearest_pixels = candidate.get("en_yakin_kenar_piksel", "-")
    nearest_m = as_float(candidate.get("en_yakin_kenar_m"))
    edge_name = str(candidate.get("en_yakin_kenar") or "kenar")
    rows.append(
        {
            "katman": "ANALİZ KENARI RİSKİ",
            "durum": "SINIRA TEMAS · YENİDEN ÖLÇ" if touches else "SINIRA YAKIN · ALARM DEĞİL",
            "bolge": candidate.get("bolge", "-"),
            "mevki": candidate.get("mahalle", "Mevki doğrulanmadı"),
            "alan_m2": int(round(area)),
            "enlem": lat,
            "boylam": lon,
            "kanıt": (
                f"En yakın {edge_name} kenarı: {nearest_pixels} piksel"
                + (f" · yaklaşık {nearest_m:.0f} m" if nearest_m is not None else "")
                + " · alan/merkez kırpma riski diagnostik"
            ),
            "son_sahne": edge_review.get("rapor_tarihi", "-"),
            "renk": [250, 205, 45, 245],
            "yaricap": 215,
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
show_blind = st.toggle(
    "Gülbahçe kalite-kör ceplerini göster",
    value=True,
    help=(
        "Turuncu noktalar şantiye sinyali değildir. Sentinel bulut/gölge/geçersiz SCL "
        "nedeniyle gözlenemeyen ve küçük bir müdahaleyi gizleyebilecek kör küme merkezleridir."
    ),
)
show_edge_risk = st.toggle(
    "Analiz kenarına yaklaşan ana adayları göster",
    value=True,
    help=(
        "Sarı noktalar yeni alarm değildir. Mevcut 250 m²+ aday bileşeninin analiz "
        "kutusu kenarına temas/yakınlık nedeniyle alan veya temsil koordinatı kırpılabilir."
    ),
)

if not signals.empty and not show_background_micro:
    signals = signals[
        ~(
            (signals["katman"] == "MİKRO 150–249")
            & (signals["durum"] == "ARKA_PLAN_TAKIP")
        )
    ].copy()
if not signals.empty and not show_blind:
    signals = signals[signals["katman"] != "GÜLBAHÇE KÖR CEP"].copy()
if not signals.empty and not show_edge_risk:
    signals = signals[signals["katman"] != "ANALİZ KENARI RİSKİ"].copy()

if signals.empty:
    st.info("Seçili katmanlarda haritada gösterilecek güçlü erken zemin sinyali yok.")
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
                    "<br><small>Koordinat sinyal/kör-küme/kenar-risk temsil noktasıdır; kesin parsel değildir.</small>"
                )
            },
        ),
        use_container_width=True,
    )
    st.caption(
        "Kırmızı = ana üretimde zaten ERKEN seçilmiş ve uydu kanıtı en fazla 2 günlük "
        "250–800 m² Sentinel adayı · Mor = 250–900 m² güçlü temporal-lokal diagnostik "
        "sinyal · Mavi/yeşil = güncel/tekrar doğrulanan MİKRO iz · Gri = eski MİKRO "
        "arka plan izi · Turuncu = Gülbahçe kalite-kör cebi · Sarı = mevcut ana adayın "
        "analiz kutusu kenarına temas/yakınlık riski. Turuncu ve sarı katmanlar alarm "
        "değildir. Harita mevcut kararları ve gözlem/ölçüm risklerini birleştirir; kendi "
        "başına yeni alarm/görev üretmez."
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
                "kanıt": "Kanıt",
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
        "Ana saha raporu:",
        latest.get("olusturma", latest.get("rapor_tarihi", "Veri yok")),
    )
    st.write(
        "Temporal-lokal kaynak:",
        temporal.get("kaynak_olusturma", temporal.get("rapor_tarihi", "Veri yok")),
    )
    st.write("MİKRO iz kaynak zamanı:", micro.get("olusturma", "Veri yok"))
    st.write("Gülbahçe kapsama denetimi:", gulbahce.get("olusturma", "Veri yok"))
    st.write(
        "Gülbahçe körlük kaynağı:",
        gulbahce_blind.get("kaynak_son_tarih", "Veri yok"),
    )
    st.write(
        "Analiz kenarı denetimi:",
        edge_review.get("rapor_tarihi", "Veri yok"),
    )
    st.caption(
        "Ada/parsel ve hukuki statü otomatik türetilmez. TKGM bağlantısı yalnız verilen "
        "koordinatı manuel Parsel Sorgu ekranında açmak içindir. Kör cep noktaları gerçek "
        "şantiye koordinatı değil, gözlenemeyen kümenin yaklaşık merkezidir. Sarı analiz "
        "kenarı kaydı da mevcut adayın şantiye olasılığını artırmaz; yalnız bbox kırpma "
        "riskini ve gerektiğinde yeniden ölçüm ihtiyacını görünür tutar."
    )