from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import streamlit as st

from field_state import ensure_state_schema, satellite_task_id, site_task_id
from scanner import connect


st.set_page_config(page_title="Saha Kontrol", page_icon="✅", layout="wide")

REPORT_FILE = Path(__file__).resolve().parents[1] / "latest_report.json"
ISSUE_URL = "https://github.com/ciselsarigurses/santiye-radari/issues/new"
STATUS_LABELS = {
    "KONTROLE_GIT": "📍 Kontrole git",
    "TEKRAR_GIT": "🔁 Bir daha git bak",
    "KONTROL_EDILDI": "✅ Kontrol edildi · listeden kaldır",
}
OUTCOME_LABELS = {
    "SANTIYE_KAZI": "🏗️ Şantiye / kazı / temel",
    "YOL_ALTYAPI": "🚧 Yol / altyapı çalışması",
    "ARAZI_BITKI": "🌿 Arazi / tarım / bitki değişimi",
    "YANLIS_POZITIF": "❌ Diğer yanlış pozitif",
}


def issue_link(task_id, status, result_code=None):
    title = f"[SAHA] {task_id} {status}"
    if result_code:
        title += f" {result_code}"
    body = (
        "Şantiye Radarı saha durum talebi. Bu kayıt otomatik işlenecektir.\n\n"
        f"Görev: {task_id}\nDurum: {status}"
    )
    if result_code:
        body += f"\nKontrol sonucu: {OUTCOME_LABELS.get(result_code, result_code)}"
    params = {"title": title, "body": body}
    return ISSUE_URL + "?" + urlencode(params)


def read_report():
    try:
        data = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def state_map():
    with connect() as connection:
        ensure_state_schema(connection)
        rows = connection.execute(
            """SELECT gorev_id,durum,kontrol_sayisi,son_islem
            FROM saha_durumlari"""
        ).fetchall()
    return {
        str(row[0]): {
            "durum": str(row[1] or "KONTROLE_GIT"),
            "kontrol_sayisi": int(row[2] or 0),
            "son_islem": row[3],
        }
        for row in rows
    }


def active_sites(states):
    with connect() as connection:
        rows = connection.execute(
            """SELECT id,durum,mahalle,ada,parsel,adres,enlem,boylam,
            firma,proje,neden,son_kontrol
            FROM santiyeler WHERE aktif=1 ORDER BY id DESC"""
        ).fetchall()
    columns = [
        "id", "durum", "mahalle", "ada", "parsel", "adres", "enlem",
        "boylam", "firma", "proje", "neden", "son_kontrol",
    ]
    result = []
    for raw in rows:
        item = dict(zip(columns, raw))
        task_id = site_task_id(item["id"])
        item["gorev_id"] = task_id
        item["saha_durumu"] = states.get(task_id, {}).get("durum", "KONTROLE_GIT")
        result.append(item)
    return result


def task_card(item, task_id, status, source_label):
    neighborhood = str(item.get("mahalle") or "Konum araştırılıyor")
    priority = str(item.get("oncelik") or item.get("durum") or "KONTROL")
    try:
        waiting_days = max(int(item.get("bekleme_gun") or 0), 0)
    except (TypeError, ValueError):
        waiting_days = 0
    overdue = bool(item.get("gecikmis"))

    with st.container(border=True):
        st.markdown(f"### {STATUS_LABELS.get(status, status)} · {neighborhood}")
        st.caption(f"Görev {task_id} · {source_label} · {priority}")

        if overdue:
            st.warning(
                f"⚠️ Bu görev {waiting_days} gündür saha kontrolü bekliyor; "
                "günün ilk rotalarından biri olmalı."
            )
        elif waiting_days > 0:
            st.caption(f"⏱️ {waiting_days} gündür saha kontrolü bekliyor.")

        if item.get("alan_m2") is not None:
            try:
                area = int(float(item.get("alan_m2") or 0))
                st.write(f"**Değişim alanı:** yaklaşık {area:,} m²".replace(",", "."))
            except (TypeError, ValueError):
                pass
        if item.get("adres"):
            st.write("**Adres:**", item["adres"])
        if item.get("ada") or item.get("parsel"):
            st.write("**Ada / Parsel:**", f"{item.get('ada') or '-'} / {item.get('parsel') or '-'}")
        if item.get("firma"):
            st.write("**Firma:**", item["firma"])
        if item.get("proje"):
            st.write("**Proje:**", item["proje"])
        if item.get("sinyal") or item.get("neden"):
            st.write("**Sinyal:**", item.get("sinyal") or item.get("neden"))

        route = item.get("harita")
        if not route and item.get("enlem") is not None and item.get("boylam") is not None:
            route = (
                "https://www.google.com/maps/dir/?api=1&destination="
                f"{item['enlem']},{item['boylam']}"
            )
        if route:
            st.link_button("🗺️ Yol tarifi", route, width="stretch")

        st.caption(
            "Durum düğmesi GitHub'da hazır bir talep açar. Açılan sayfada yalnızca "
            "yeşil ‘Submit new issue’ düğmesine bas; radar kaydı otomatik günceller."
        )
        c1, c2, c3 = st.columns(3)
        c1.link_button(
            "📍 Kontrole git",
            issue_link(task_id, "KONTROLE_GIT"),
            width="stretch",
        )
        c2.link_button(
            "🔁 Bir daha git bak",
            issue_link(task_id, "TEKRAR_GIT"),
            width="stretch",
        )
        c3.link_button(
            "✅ Kontrol edildi",
            issue_link(task_id, "KONTROL_EDILDI"),
            width="stretch",
        )

        with st.expander("🧾 Sonuçla kapat · yanlış pozitifleri öğren"):
            st.caption(
                "Mümkünse genel ‘Kontrol edildi’ yerine gerçek saha sonucunu seç. "
                "Bu sınıflar ileride uydu yanlış pozitiflerini azaltmak için veri olarak saklanır."
            )
            r1, r2, r3, r4 = st.columns(4)
            result_buttons = [
                (r1, "🏗️ Şantiye / kazı", "SANTIYE_KAZI"),
                (r2, "🚧 Yol / altyapı", "YOL_ALTYAPI"),
                (r3, "🌿 Arazi / bitki", "ARAZI_BITKI"),
                (r4, "❌ Yanlış pozitif", "YANLIS_POZITIF"),
            ]
            for column, label, result_code in result_buttons:
                column.link_button(
                    label,
                    issue_link(task_id, "KONTROL_EDILDI", result_code),
                    width="stretch",
                )


st.title("✅ Saha Kontrol Merkezi")
st.caption("Git · tekrar kontrol et · kontrol edildi kararlarını kalıcı olarak yönet.")
st.info(
    "‘Kontrol edildi’ seçilen nokta aktif listeden çıkar; geçmiş kaydı silinmez. "
    "‘Bir daha git bak’ seçilen nokta takip listesinde en öne alınır."
)

report = read_report()
states = state_map()

satellite_items = []
for raw in report.get("saha_adaylari", []):
    if not isinstance(raw, dict):
        continue
    item = dict(raw)
    try:
        task_id = str(item.get("gorev_id") or satellite_task_id(item))
    except (TypeError, ValueError):
        continue
    status = states.get(task_id, {}).get(
        "durum", str(item.get("saha_durumu") or "KONTROLE_GIT")
    )
    if status == "KONTROL_EDILDI":
        continue
    item["gorev_id"] = task_id
    item["saha_durumu"] = status
    satellite_items.append(item)

site_items = [
    item for item in active_sites(states)
    if item.get("saha_durumu") != "KONTROL_EDILDI"
]

repeat_total = sum(
    item.get("saha_durumu") == "TEKRAR_GIT"
    for item in satellite_items + site_items
)
overdue_total = sum(bool(item.get("gecikmis")) for item in satellite_items)

m1, m2, m3, m4 = st.columns(4)
m1.metric("🛰️ Uydu görevi", len(satellite_items))
m2.metric("🎯 Kayıtlı saha", len(site_items))
m3.metric("⚠️ Geciken", overdue_total)
m4.metric("🔁 Tekrar gidilecek", repeat_total)

repeat_tab, satellite_tab, site_tab, history_tab = st.tabs(
    ["🔁 Tekrar Git", "🛰️ Uydu Adayları", "🎯 Saha Kayıtları", "✅ Kontrol Edilenler"]
)

with repeat_tab:
    repeated = [
        (item, "Uydu") for item in satellite_items
        if item.get("saha_durumu") == "TEKRAR_GIT"
    ] + [
        (item, "Saha listesi") for item in site_items
        if item.get("saha_durumu") == "TEKRAR_GIT"
    ]
    if not repeated:
        st.info("Henüz ‘Bir daha git bak’ olarak işaretlenmiş kayıt yok.")
    for item, source in repeated:
        task_card(item, item["gorev_id"], "TEKRAR_GIT", source)

with satellite_tab:
    if not satellite_items:
        st.success("Aktif uydu saha görevi yok.")
    for item in satellite_items:
        task_card(
            item,
            item["gorev_id"],
            item.get("saha_durumu", "KONTROLE_GIT"),
            "Sentinel-2",
        )

with site_tab:
    if not site_items:
        st.info("Aktif kayıtlı saha görevi yok.")
    for item in site_items:
        task_card(
            item,
            item["gorev_id"],
            item.get("saha_durumu", "KONTROLE_GIT"),
            "Saha listesi",
        )

with history_tab:
    completed = []
    with connect() as connection:
        ensure_state_schema(connection)
        completed = connection.execute(
            """SELECT gorev_id,kaynak,mahalle,kontrol_sayisi,sonuc,son_islem
            FROM saha_durumlari WHERE durum='KONTROL_EDILDI'
            ORDER BY son_islem DESC LIMIT 200"""
        ).fetchall()
    if not completed:
        st.info("Henüz kontrol edilip kapatılan kayıt yok.")
    else:
        history_rows = [
            (
                row[0],
                row[1],
                row[2],
                row[3],
                OUTCOME_LABELS.get(str(row[4] or ""), "Sonuç belirtilmedi"),
                row[5],
            )
            for row in completed
        ]
        history = pd.DataFrame(
            history_rows,
            columns=["Görev", "Kaynak", "Mahalle", "Kontrol sayısı", "Sonuç", "Son işlem"],
        )
        st.dataframe(history, hide_index=True, width="stretch")
