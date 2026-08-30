"""Arama sıralaması dalgalanmasında aday yaşam döngüsünü dengeler.

Ana taramaya ek olarak Çeşme Belediyesi'nin güncel mahalle listesini hedefleyen,
mevcut sonuçları pasife almayan tamamlayıcı bir internet taraması çalıştırır. Bu
katman uydu alarmı üretmez; yalnız erken hafriyat/temel sinyallerinin web tarafında
mahalle adı nedeniyle kaçmasını azaltır.

Google Haberler bazen aylar önce gerçekleşmiş temel atma/hafriyat başlangıcı
haberlerini yeniden üst sıralara taşıyabiliyor. Erken şantiye hedefi açısından bunlar
"bugün yeni" değildir. Yayın tarihi açıkça 90 günden eski olan ve geçmişte kalmış
başlangıç eylemi anlatan sonuçlar ek mahalle taramasında yeniden aktive edilmez.
"""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

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

STALE_START_EVENT_DAYS = 90
STALE_START_EVENT_PHRASES = (
    "temel atma töreni",
    "temeli atıldı",
    "temel atıldı",
    "hafriyat başladı",
    "hafriyata başlandı",
    "inşaata başladı",
    "inşaata başlandı",
    "şantiye kuruldu",
)
SUPPLEMENTAL_MAX_WORKERS = 3
SUPPLEMENTAL_RETRY_ATTEMPTS = 2
SUPPLEMENTAL_RETRY_DELAY_SECONDS = 1.5


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


def _published_age_days(published, now=None):
    if not published:
        return None
    try:
        published_date = parsedate_to_datetime(published)
        if published_date.tzinfo is None:
            published_date = published_date.replace(tzinfo=timezone.utc)
        reference = now or datetime.now(timezone.utc)
        return max((reference - published_date).days, 0)
    except (TypeError, ValueError, OverflowError):
        return None


def _stale_start_event(raw, now=None):
    """Yayın tarihi açıkça eski ve geçmiş başlangıç eylemi olan haberi tanır."""
    age_days = _published_age_days(raw.get("published"), now=now)
    if age_days is None or age_days <= STALE_START_EVENT_DAYS:
        return False
    text = scanner._plain(
        f"{raw.get('title', '')} {raw.get('snippet', '')}"
    ).casefold()
    return any(phrase in text for phrase in STALE_START_EVENT_PHRASES)


def _deactivate_urls(urls):
    urls = sorted({str(url or "").strip() for url in urls if str(url or "").strip()})
    if not urls:
        return 0
    placeholders = ",".join("?" for _ in urls)
    with scanner.connect() as connection:
        cursor = connection.execute(
            f"""UPDATE internet_adaylari SET aktif=0
            WHERE kaynak_tipi IS NOT NULL AND kaynak_url IN ({placeholders})""",
            urls,
        )
        return max(cursor.rowcount or 0, 0)


def _new_session():
    session = requests.Session()
    session.headers.update(
        {"User-Agent": scanner.USER_AGENT, "Accept-Language": "tr-TR,tr;q=0.9"}
    )
    return session


def _search_with_retry(engine, query, attempts=SUPPLEMENTAL_RETRY_ATTEMPTS, delay=SUPPLEMENTAL_RETRY_DELAY_SECONDS):
    """Arama motorunu bağımsız oturumla sınırlı sayıda yeniden dene.

    ``requests.Session`` nesnesini eşzamanlı görevler arasında paylaşmıyoruz. İlk
    denemede geçici HTTP/timeout hatası olursa yalnız bir kez, kısa gecikmeyle
    yeniden denenir. Kalıcı hata sessizce yutulmaz; çağırana hata tipi döner.
    """
    tries = max(int(attempts), 1)
    last_error = None
    for attempt in range(1, tries + 1):
        session = _new_session()
        try:
            return list(engine(session, query)), attempt, None
        except Exception as exc:
            last_error = exc
        finally:
            session.close()
        if attempt < tries and delay > 0:
            time.sleep(float(delay))
    return [], tries, type(last_error).__name__ if last_error is not None else "UnknownError"


def _self_check():
    reference = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    assert _stale_start_event(
        {
            "title": "Örnek proje temel atma töreni yapıldı",
            "snippet": "",
            "published": "Thu, 02 Apr 2026 12:00:00 GMT",
        },
        now=reference,
    )
    assert not _stale_start_event(
        {
            "title": "Örnek proje temel atma töreni yapıldı",
            "snippet": "",
            "published": "Sat, 29 Aug 2026 12:00:00 GMT",
        },
        now=reference,
    )
    assert not _stale_start_event(
        {
            "title": "Şantiye çalışmaları sürüyor",
            "snippet": "",
            "published": "Thu, 02 Apr 2026 12:00:00 GMT",
        },
        now=reference,
    )

    calls = {"count": 0}

    def flaky_engine(session, query):
        del session, query
        calls["count"] += 1
        if calls["count"] == 1:
            raise requests.Timeout("geçici")
        return [{"title": "ok"}]

    rows, attempts, error = _search_with_retry(
        flaky_engine,
        "test",
        attempts=2,
        delay=0,
    )
    assert rows == [{"title": "ok"}]
    assert attempts == 2
    assert error is None


def supplemental_neighborhood_scan():
    """Resmi mahallelerin tamamını düşük-riskli ek aramayla tara.

    Sonuç depolamada ``deactivate_missing=False`` kullanılır; bu nedenle ana
    taramadaki adayları bu ek tarama görmedi diye kapatmaz. Aynı URL yeniden
    bulunursa daha özgül mahalle etiketi mevcut kayda güvenli biçimde yazılabilir.
    Yayın tarihi 90 günden eski geçmiş başlangıç haberleri ise gözlemlendiği URL
    üzerinden ayrıca pasifleştirilir; güncel devam eden şantiye haberleri korunur.
    """
    _configure_specific_locations()
    scanner.ensure_schema()

    queries = [_query_for(name) for name in OFFICIAL_NEIGHBORHOOD_QUERIES]
    queries.append(
        'Uzunkuyu Urla "hafriyat" OR "temel" OR "yeni inşaat" OR "şantiye" OR "villa projesi"'
    )

    found = {}
    stale_urls = set()
    errors = []
    retry_recovered = 0
    jobs = []

    with ThreadPoolExecutor(max_workers=SUPPLEMENTAL_MAX_WORKERS) as pool:
        for query in queries:
            for engine_name, engine in (
                ("Google Haberler", scanner._google_news),
                ("Web", scanner._duckduckgo),
            ):
                jobs.append(
                    (query, engine_name, pool.submit(_search_with_retry, engine, query))
                )

        for query, engine_name, future in jobs:
            raw_rows, attempts, error_name = future.result()
            if error_name:
                errors.append(f"{engine_name} / {query}: {error_name}")
                continue
            if attempts > 1:
                retry_recovered += 1
            for raw in raw_rows:
                url = scanner._canonical_url(raw.get("url", ""))
                if _stale_start_event(raw):
                    if url:
                        stale_urls.add(url)
                    continue
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
        "retry_recovered": retry_recovered,
        "repaired_labels": repaired_labels,
        "stale_urls": sorted(stale_urls),
    }


def retain_recent_candidates():
    """Mahalle kapsamasını tamamla, eski başlangıç haberini bastır ve toleransı koru."""
    _self_check()
    supplemental = supplemental_neighborhood_scan()
    retention = scanner.apply_candidate_retention()
    # Retention bir önceki turda görülen adayı tek tarama boyunca geri açabilir.
    # Açık yayın tarihiyle eski olduğu bu turda tekrar doğrulanan başlangıç haberi
    # bu toleranstan sonra yeniden pasifleştirilir.
    stale_deactivated = _deactivate_urls(supplemental.get("stale_urls", []))
    supplemental["stale_deactivated"] = stale_deactivated
    supplemental.pop("stale_urls", None)
    return {"supplemental": supplemental, "retention": retention}


if __name__ == "__main__":
    result = retain_recent_candidates()
    supplemental = result["supplemental"]
    retention = result["retention"]
    print(
        "Resmi mahalle ek taraması: "
        f"{supplemental['found']} uygun sonuç, {supplemental['new']} yeni, "
        f"{supplemental['updated']} güncellendi, "
        f"{supplemental['repaired_labels']} kaynak etiketi düzeltildi; "
        f"{supplemental['stale_deactivated']} eski başlangıç haberi pasifleştirildi."
    )
    if supplemental["retry_recovered"]:
        print(
            f"Ek mahalle taramasında {supplemental['retry_recovered']} geçici kaynak/arama hatası "
            "sınırlı yeniden denemeyle kurtarıldı."
        )
    if supplemental["errors"]:
        print(
            f"Ek mahalle taramasında {len(supplemental['errors'])} kaynak/arama hatası "
            "yeniden denemeden sonra da kaldı; ana tarama adayları pasife alınmadı."
        )
    if retention["skipped"]:
        print("Aday kalıcılığı: veri güvenliği nedeniyle bu turda aktiflik değiştirilmedi.")
    else:
        print(
            "Aday kalıcılığı: önceki taramada görülüp bu turda kaybolan "
            f"{retention['retained']} kayıt korundu; "
            f"iki taramadır görünmeyen {retention['deactivated']} kayıt pasife alındı."
        )
