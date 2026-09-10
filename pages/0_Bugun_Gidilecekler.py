from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode

import streamlit as st

from coordinate_navigation import load_audit, signal_core_target
from portal_route_policy import is_curated_portal_actionable, portal_issue_title


st.set_page_config(page_title="Bugün Gidilecekler", page_icon="📍", layout="wide")

REPORT_FILE = Path(__file__).resolve().parents[1] / "latest_report.json"
ISSUE_URL = "https://github.com/ciselsarigurses/santiye-radari/issues/new"
MAX_DAILY = 10
ACTIVE_STATUSES = {"KONTROLE_GIT", "TEKRAR_GIT"}
COORDINATE_AUDIT = load_audit()


def load_report() -> dict:
    try:
        data = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def parse_date(value):
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def evidence_age(item: dict, report_day: date) -> int | None:
    raw = item.get("uydu_kanit_yasi_gun")
    try:
        if raw is not None:
            return max(int(raw), 0)
    except (TypeError, ValueError):
        pass
    scene_day = parse_date(item.get("son_tarih"))
    return max((report_day - scene_day).days, 0) if scene_day else None


def is_today_candidate(item: dict, report_day: date) -> bool:
    status = str(item.get("saha_durumu") or "KONTROLE_GIT").upper()
    if status not in ACTIVE_STATUSES:
        return False
    if status == "TEKRAR_GIT":
        return True

    priority = str(item.get("oncelik") or "").upper()
    age = evidence_age(item, report_day)
    recent = age is not None and age <= 2
    fresh_priority = any(
        token in priority
        for token in ("ERKEN", "PARSEL", "TAZE", "DOĞRULAMA", "DOGRULAMA")
    )

    # Eski raporlarda henüz merkezi günlük rota alanı yoksa güvenli fallback.
    # Yeni raporlarda portal aşağıdaki `gunun_ilk_3_kontrolu` sonucunu doğrudan
    # kullanır; böylece GECİKEN etiketli olsa bile güncel, küçük-güçlü Sentinel
    # adayı portal tarafından yanlışlıkla elenmez.
    return bool(item.get("yeni_goruntu")) or (recent and fresh_priority)


def sort_key(item: dict, report_day: date):
    status = str(item.get("saha_durumu") or "KONTROLE_GIT").upper()
    priority = str(item.get("oncelik") or "").upper()
    age = evidence_age(item, report_day)
    area = item.get("alan_m2")
    try:
        area_num = float(area or 0)
    except (TypeError, ValueError):
        area_num = 0
    rank = 0 if status == "TEKRAR_GIT" else 1
    if "TAZE" in priority:
        rank = min(rank, 1)
    elif "ERKEN" in priority:
        rank += 1
    elif "PARSEL" in priority:
        rank += 2
    else:
        rank += 3
    return (rank, age if age is not None else 99, area_num)


def portal_items(report: dict, report_day: date) -> tuple[list[dict], bool]:
    """Merkezi saha rotasını kullan; yalnız eski raporlarda yerel fallback uygula.

    Merkezi rota normal KONTROLE_GIT/TEKRAR_GIT kayıtlarının yanında yalnız
    15 Eylül sonrası kuru-zemin korumasının açıkça işaretlediği 250-900 m²
    tek-günlük DIAGNOSTIK_DOGRULAMA kaydını gösterebilir. 150-249 m² MİKRO ve
    diğer diagnostik/arka-plan kayıtları portal görevi haline gelmez.
    """
    if "gunun_ilk_3_kontrolu" in report:
        curated = []
        for raw in report.get("gunun_ilk_3_kontrolu") or []:
            if not isinstance(raw, dict):
                continue
            if not is_curated_portal_actionable(raw):
                continue
            curated.append(dict(raw))
        curated.sort(
            key=lambda item: (
                int(item.get("gunluk_sira") or 999),
                sort_key(item, report_day),
            )
        )
        return curated[:MAX_DAILY], True

    legacy = [
        dict(item)
        for item in report.get("saha_adaylari", [])
        if isinstance(item, dict) and is_today_candidate(item, report_day)
    ]
    legacy.sort(key=lambda item: sort_key(item, report_day))
    return legacy[:MAX_DAILY], False


def issue_url(task_id: str, action: str, name: str = "", phone: str = "", note: str = "") -> str | None:
    try:
        title = portal_issue_title(task_id, action)
    except ValueError:
        return None
    body = [
        "Şantiye Radarı sade saha paneli talebi.",
        "",
        f"Görev: {task_id}",
        f"İşlem: {action}",
    ]
    if name:
        body.append(f"Potansiyel müşteri / firma: {name}")
    if phone:
        body.append(f"İletişim: {phone}")
    if note:
        body.append(f"Not: {note}")
    return ISSUE_URL + "?" + urlencode({"title": title, "body": "\n".join(body)})


def map_url(item: dict) -> str | None:
    if item.get("harita"):
        return str(item["harita"])
    if item.get("enlem") is not None and item.get("boylam") is not None:
        return f"https://www.google.com/maps/dir/?api=1&destination={item['enlem']},{item['boylam']}"
    return None


st.markdown(
    """
    <style>
      .block-container {max-width: 1050px; padding-top: 1.4rem;}
      [data-testid="stHeader"] {background: transparent;}
      .sr-card {border:1px solid #e5e7eb;border-radius:16px;padding:16px 18px;margin:10px 0;background:white;}
      .sr-rank {font-size:13px;font-weight:700;letter-spacing:.08em;color:#991b1b;}
      .sr-title {font-size:24px;font-weight:750;margin:3px 0 6px 0;}
      .sr-meta {color:#4b5563;font-size:14px;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("📍 Bugün Gidilecekler")
st.caption("Sarıoğlu Yapı · Şantiye Radarı saha ekranı")

report = load_report()
report_day = parse_date(report.get("rapor_tarihi")) or date.today()
items, curated_route = portal_items(report, report_day)

c1, c2 = st.columns(2)
c1.metric("Bugün gidilecek", len(items))
c2.metric("Rapor", report_day.strftime("%d.%m.%Y"))

if not items:
    st.success("Bugün için güçlü yeni kazı / toprak hareketi adayı yok.")
    st.caption("Eski backlog bu ekranda günlük rota olarak gösterilmez.")
else:
    st.info("Sırayla kontrol edin. Konuma gidin; sahada sonucu yalnız Doğru Adres – Takip Et, Potansiyel Müşteri veya Çöp Adres olarak işaretleyin.")
    if curated_route:
        st.caption("Liste radarın merkezi ‘Günün ilk 3 kontrolü’ kararından gelir; portal ayrıca yalnız güvenli saha eylemi filtresini uygular.")

for index, item in enumerate(items, start=1):
    task_id = str(item.get("gorev_id") or f"RADAR-{index}")
    neighborhood = str(item.get("mahalle") or "Mevki doğrulanmadı")
    try:
        area = int(float(item.get("alan_m2") or 0))
        area_text = f"~{area:,} m²".replace(",", ".")
    except (TypeError, ValueError):
        area_text = "Alan ölçülmedi"
    signal = str(item.get("sinyal") or item.get("oncelik_nedeni") or "Yeni zemin hareketi")
    age = evidence_age(item, report_day)

    st.markdown(
        f"""
        <div class="sr-card">
          <div class="sr-rank">ÖNCELİK {index}</div>
          <div class="sr-title">{neighborhood} · {area_text}</div>
          <div class="sr-meta">{signal}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if age is not None:
        st.caption(f"Uydu kanıt yaşı: {age} gün · Görev: {task_id}")

    route = map_url(item)
    if route:
        st.link_button("📍 KONUMA GİT", route, use_container_width=True)

    # Ana koordinatı değiştirmeden, aynı güncel Sentinel bileşeninde ölçülen ve
    # en fazla 20 m kaymış daha güçlü piksel varsa saha ekibine ikinci bir hedef
    # ver. Bu yalnız 250 m²+ taze ana adaylarda çalışır; MİKRO/arka-plan kayıtlarını
    # yükseltmez ve kesin adres/parsel iddiası oluşturmaz.
    signal_target = signal_core_target(item, COORDINATE_AUDIT)
    if signal_target:
        st.caption(
            "🎯 Sinyal çekirdeği: aynı Sentinel değişim kümesindeki daha güçlü piksel; "
            f"ana noktadan yaklaşık {signal_target['kayma_m']:.0f} m. Kesin adres/parsel değildir."
        )
        st.link_button(
            "🎯 SİNYAL ÇEKİRDEĞİNE GİT",
            signal_target["harita"],
            use_container_width=True,
        )

    follow_url = issue_url(task_id, "DOGRU_ADRES_TAKIP")
    trash_url = issue_url(task_id, "COP_ADRES_KALDIR")
    if follow_url and trash_url:
        a, b = st.columns(2)
        a.link_button(
            "📌 DOĞRU ADRES – TAKİP ET",
            follow_url,
            use_container_width=True,
        )
        b.link_button(
            "🗑️ ÇÖP ADRES / KALDIR",
            trash_url,
            use_container_width=True,
        )
    else:
        st.warning("Bu kaydın doğrulanmış görev kimliği yok; yanlış saha durumuna yazmamak için karar düğmeleri kapalı.")

    with st.expander("⭐ Potansiyel müşteri olarak kaydet"):
        with st.form(f"potential_{task_id}"):
            name = st.text_input("Müşteri / firma adı", key=f"name_{task_id}")
            phone = st.text_input("Telefon / iletişim", key=f"phone_{task_id}")
            note = st.text_input("Kısa not (opsiyonel)", key=f"note_{task_id}")
            prepared = st.form_submit_button("Potansiyel müşteri kaydını hazırla", use_container_width=True)
        if prepared:
            if not name.strip() and not phone.strip():
                st.warning("En az müşteri/firma adı veya iletişim bilgisi gir.")
            else:
                potential_url = issue_url(
                    task_id,
                    "POTANSIYEL_MUSTERI",
                    name.strip(),
                    phone.strip(),
                    note.strip(),
                )
                if potential_url:
                    st.link_button(
                        "⭐ POTANSİYEL MÜŞTERİ OLARAK KAYDET",
                        potential_url,
                        use_container_width=True,
                    )
                    st.caption("Potansiyel müşteri kaydı ayrı GitHub talebi olarak tutulur; Sentinel saha kalibrasyon sonucunu değiştirmez.")
                else:
                    st.warning("Doğrulanmış görev kimliği olmadığı için müşteri kaydı bu radar noktasına bağlanmadı.")

    st.divider()

st.caption("Teknik Sentinel / MİKRO / workflow ekranları saha personeline gösterilmez. Bu sayfa yalnız günlük operasyon içindir.")
