"""Arama sıralaması dalgalanmasında aday yaşam döngüsünü dengeler.

Ana taramaya ek olarak Çeşme Belediyesi'nin güncel mahalle listesini hedefleyen,
mevcut sonuçları pasife almayan tamamlayıcı bir internet taraması çalıştırır. Bu
katman uydu alarmı üretmez; yalnız erken hafriyat/temel sinyallerinin web tarafında
mahalle adı nedeniyle kaçmasını azaltır.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor

import requests

import scanner
from source_label_quality import repair_database


# Çeşme Belediyesi "Mahalle ve Muhtarlar" sayfasındaki güncel mahalleler.
# Etiketlerde mevcut uygulama dili korunur: Çiftlik -> Çiftlikköy, Ildırı -> Ildır,
# Onaltı Eylül -> 16 Eylül. Uzunkuyu proje kapsamı gereği ayrıca eklenir.
OFFICIAL_LOCATION_ALIASES = {
    "alaçatı": "Alaçatı",
    "alacati": "Alaçatı",
    "altınkum": "Altınkum",
    "altinkum": "Altınkum",
    "altınyunus": "Altınyunus",
    "altinyunus": "Altınyunus",
    "ardıç": "Ardıç",
    "ardic": "Ardıç",
    "boyalık": "Boyalık",
    "boyalik": "Boyalık",
    "celal bayar": "Celal Bayar",
    "cumhuriyet": "Cumhuriyet",
    "çakabey": "Çakabey",
    "cakabey": "Çakabey",
    "çiftlikköy": "Çiftlikköy",
    "ciftlikkoy": "Çiftlikköy",
    "çiftlik": "Çiftlikköy",
    "ciftlik": "Çiftlikköy",
    "dalyan": "Dalyan",
    "fahrettinpaşa": "Fahrettinpaşa",
    "fahrettin paşa": "Fahrettinpaşa",
    "fahrettinpasa": "Fahrettinpaşa",
    "germiyan": "Germiyan",
    "ıldırı": "Ildır",
    "ildiri": "Ildır",
    "ıldır": "Ildır",
    "ildir": "Ildır",
    "ılıca": "Ilıca",
    "ilica": "Ilıca",
    "ismet inönü": "İsmet İnönü",
    "ismet inonu": "İsmet İnönü",
    "iṡmet i̇nönü": "İsmet İnönü",
    "inönü": "İsmet İnönü",
    "inonu": "İsmet İnönü",
    "karaköy": "Karaköy",
    "karakoy": "Karaköy",
    "musalla": "Musalla",
    "onaltı eylül": "16 Eylül",
    "onalti eylul": "16 Eylül",
    "16 eylül": "16 Eylül",
    "16 eylul": "16 Eylül",
    "ovacık": "Ovacık",
    "ovacik": "Ovacık",
    "reisdere": "Reisdere",
    "sakarya": "Sakarya",
    "şehit mehmet": "Şehit Mehmet",
    "sehit mehmet": "Şehit Mehmet",
    "şifne": "Şifne",
    "sifne": "Şifne",
    "üniversite": "Üniversite",
    "universite": "Üniversite",
    "yalı": "Yalı",
    "yali": "Yalı",
    "uzunkuyu": "Uzunkuyu",
    "paşalimanı": "Paşalimanı",
    "pasalimani": "Paşalimanı",
}

OFFICIAL_NEIGHBORHOOD_QUERIES = [
    "Alaçatı", "Altınkum", "Altınyunus", "Ardıç", "Boyalık", "Celal Bayar",
    "Cumhuriyet", "Çakabey", "Çiftlik", "Dalyan", "Fahrettinpaşa", "Germiyan",
    "Ildırı", "Ilıca", "İsmet İnönü", "Karaköy", "Musalla", "Onaltı Eylül",
    "Ovacık", "Reisdere", "Sakarya", "Şehit Mehmet", "Şifne", "Üniversite", "Yalı",
]


def _configure_specific_locations():
    """Özgül mahalleleri genel 'Çeşme' eşleşmesinden önce değerlendir.

    ``scanner._evaluate`` ilk eşleşen konumu kullanır. Genel 'Çeşme' anahtarı
    listenin başındaysa 'Ovacık Çeşme ...' gibi bir sonuç yanlışlıkla Çeşme diye
    etiketlenebilir. Burada resmi mahalleler önce, ilçe adı en son yerleştirilir.
    """
    locations = dict(OFFICIAL_LOCATION_ALIASES)
    locations["çeşme"] = "Çeşme"
    locations["cesme"] = "Çeşme"
    scanner.LOCATIONS = locations


def _query_for(neighborhood):
    return (
        f'{neighborhood} Çeşme "hafriyat" OR "temel" OR "yeni inşaat" '
        f'OR "şantiye" OR "villa projesi"'
    )


def supplemental_neighborhood_scan():
    """Resmi mahallelerin tamamını düşük-riskli ek aramayla tara.

    Sonuç depolamada ``deactivate_missing=False`` kullanılır; bu nedenle ana
    taramadaki adayları bu ek tarama görmedi diye kapatmaz. Aynı URL yeniden
    bulunursa daha özgül mahalle etiketi mevcut kayda güvenli biçimde yazılabilir.
    """
    _configure_specific_locations()
    scanner.ensure_schema()

    queries = [_query_for(name) for name in OFFICIAL_NEIGHBORHOOD_QUERIES]
    queries.append(
        'Uzunkuyu Urla "hafriyat" OR "temel" OR "yeni inşaat" OR "şantiye" OR "villa projesi"'
    )

    session = requests.Session()
    session.headers.update(
        {"User-Agent": scanner.USER_AGENT, "Accept-Language": "tr-TR,tr;q=0.9"}
    )
    found = {}
    errors = []
    jobs = []

    with ThreadPoolExecutor(max_workers=6) as pool:
        for query in queries:
            for engine_name, engine in (
                ("Google Haberler", scanner._google_news),
                ("Web", scanner._duckduckgo),
            ):
                jobs.append((query, engine_name, pool.submit(engine, session, query)))

        for query, engine_name, future in jobs:
            try:
                for raw in future.result():
                    url = scanner._canonical_url(raw.get("url", ""))
                    candidate = scanner._evaluate(
                        raw.get("title", ""),
                        raw.get("snippet", ""),
                        url,
                        raw.get("published"),
                    )
                    if not candidate:
                        continue
                    key = hashlib.sha1(
                        candidate["kaynak_url"].encode("utf-8")
                    ).hexdigest()
                    old = found.get(key)
                    if not old or candidate["skor"] > old["skor"]:
                        found[key] = candidate
            except Exception as exc:
                errors.append(f"{engine_name} / {query}: {type(exc).__name__}")

    new_count, updated_count = scanner._store(
        list(found.values()),
        deactivate_missing=False,
    )
    repaired_labels = repair_database()
    return {
        "found": len(found),
        "new": new_count,
        "updated": updated_count,
        "errors": errors,
        "repaired_labels": repaired_labels,
    }


def retain_recent_candidates():
    """Mahalle kapsamasını tamamla ve mevcut tek-tur toleransını koru."""
    supplemental = supplemental_neighborhood_scan()
    retention = scanner.apply_candidate_retention()
    return {"supplemental": supplemental, "retention": retention}


if __name__ == "__main__":
    result = retain_recent_candidates()
    supplemental = result["supplemental"]
    retention = result["retention"]
    print(
        "Resmi mahalle ek taraması: "
        f"{supplemental['found']} uygun sonuç, {supplemental['new']} yeni, "
        f"{supplemental['updated']} güncellendi, "
        f"{supplemental['repaired_labels']} kaynak etiketi düzeltildi."
    )
    if supplemental["errors"]:
        print(
            f"Ek mahalle taramasında {len(supplemental['errors'])} geçici kaynak/arama hatası oluştu; "
            "ana tarama adayları pasife alınmadı."
        )
    if retention["skipped"]:
        print("Aday kalıcılığı: veri güvenliği nedeniyle bu turda aktiflik değiştirilmedi.")
    else:
        print(
            "Aday kalıcılığı: önceki taramada görülüp bu turda kaybolan "
            f"{retention['retained']} kayıt korundu; "
            f"iki taramadır görünmeyen {retention['deactivated']} kayıt pasife alındı."
        )
