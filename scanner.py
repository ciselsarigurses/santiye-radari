import hashlib
import re
import sqlite3
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


DB = Path(__file__).with_name("santiye.db")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 "
    "SantiyeRadari/2.0"
)

SEARCH_QUERIES = [
    'Çeşme "yeni inşaat"',
    'Çeşme "inşaata başladı" OR "temel atıldı"',
    'Çeşme "villa projesi"',
    'Alaçatı "villa projesi" OR şantiye',
    'Ilıca Çeşme yeni proje inşaat',
    'Reisdere Çeşme inşaat villa',
    'Ovacık Çeşme inşaat villa',
    'Dalyan Çeşme yeni inşaat',
    'Çiftlikköy Çeşme yeni inşaat',
    'Musalla Çeşme yeni inşaat',
    'Şifne Çeşme yeni inşaat villa',
    'Germiyan Çeşme yeni inşaat villa',
    'Ildır Çeşme yeni inşaat villa',
    'Paşalimanı Çeşme yeni inşaat villa',
    'Çakabey Çeşme yeni inşaat villa',
    # Çeşme Belediyesi'nin güncel mahalle/muhtar listesinde yer alan ve önceki
    # dar sorgu setinde ayrı aranmayan mahalleler. Amaç alarm üretmek değil,
    # mahalle bazında açık-web doğrulama körlüğünü azaltmaktır.
    'Altınkum Çeşme yeni inşaat villa hafriyat',
    'Altınyunus Çeşme yeni inşaat villa',
    'Ardıç Çeşme yeni inşaat villa hafriyat',
    'Boyalık Çeşme yeni inşaat villa',
    'Celal Bayar Çeşme yeni inşaat villa',
    'Cumhuriyet Çeşme yeni inşaat villa hafriyat',
    'Fahrettinpaşa Çeşme yeni inşaat villa',
    'İsmet İnönü Çeşme yeni inşaat villa',
    'Karaköy Çeşme yeni inşaat villa hafriyat',
    '16 Eylül Çeşme yeni inşaat otel villa',
    'Sakarya Çeşme yeni inşaat villa',
    'Şehit Mehmet Çeşme yeni inşaat villa',
    'Üniversite Çeşme yeni inşaat villa',
    'Yalı Çeşme yeni inşaat villa hafriyat',
    'Uzunkuyu Urla inşaat proje',
    'site:cesme.bel.tr "yapı ruhsatı" Çeşme',
    'site:cesme.bel.tr "inşaata başlama" OR "temel atıldı"',
    'Çeşme "ruhsat aldı" OR "yapı ruhsatı aldı"',
    'Çeşme "hafriyat başladı" OR "şantiye kuruldu"',
    'Çeşme "inşaata başlayacak" müteahhit proje',
    'site:cesme.bel.tr inşaat ruhsat ihale',
    'site:instagram.com Çeşme inşaat villa',
    'site:instagram.com Alaçatı şantiye villa',
    'site:instagram.com Uzunkuyu inşaat',
]

INSTAGRAM_SEARCH_LINKS = [
    ("Çeşme inşaat", "https://www.google.com/search?q=" + quote_plus("site:instagram.com Çeşme inşaat villa")),
    ("Alaçatı şantiye", "https://www.google.com/search?q=" + quote_plus("site:instagram.com Alaçatı şantiye villa")),
    ("Uzunkuyu inşaat", "https://www.google.com/search?q=" + quote_plus("site:instagram.com Uzunkuyu inşaat")),
    ("#çeşmeinşaat", "https://www.instagram.com/explore/tags/cesmeinsaat/"),
    ("#alaçatıvilla", "https://www.instagram.com/explore/tags/alacativilla/"),
]

LOCATIONS = {
    # Önce spesifik mahalle/bölge anahtarları tutulur. "Çeşme" ilçe adı çoğu
    # sonuçta mahalle adının yanında geçtiği için genel eşleşme en son fallback
    # olmalıdır; aksi halde "Ovacık Çeşme" gibi kayıtlar yanlışlıkla Çeşme olur.
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
    "çiftlik mahallesi": "Çiftlikköy",
    "ciftlik mahallesi": "Çiftlikköy",
    "dalyan": "Dalyan",
    "fahrettinpaşa": "Fahrettinpaşa",
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
    "inönü mahallesi": "İsmet İnönü",
    "inonu mahallesi": "İsmet İnönü",
    "karaköy": "Karaköy",
    "karakoy": "Karaköy",
    "musalla": "Musalla",
    "onaltı eylül": "16 Eylül",
    "onalti eylul": "16 Eylül",
    "16 eylül": "16 Eylül",
    "16 eylul": "16 Eylül",
    "ovacık": "Ovacık",
    "ovacik": "Ovacık",
    "paşalimanı": "Paşalimanı",
    "pasalimani": "Paşalimanı",
    "reisdere": "Reisdere",
    "sakarya": "Sakarya",
    "şehit mehmet": "Şehit Mehmet",
    "sehit mehmet": "Şehit Mehmet",
    "şifne": "Şifne",
    "sifne": "Şifne",
    "üniversite": "Üniversite",
    "universite": "Üniversite",
    "uzunkuyu": "Uzunkuyu",
    "yalı": "Yalı",
    "yali": "Yalı",
    "çeşme": "Çeşme",
    "cesme": "Çeşme",
}

SIGNALS = {
    "ruhsat": 4,
    "yapı ruhsatı": 5,
    "temel at": 5,
    "hafriyat": 4,
    "şantiye kuruldu": 5,
    "şantiye çalışmaları": 4,
    "şantiye alanı": 4,
    "şantiye": 1,
    "inşaata baş": 5,
    "inşaat baş": 5,
    "kaba inşaat": 5,
    "yapım işi": 3,
    "yapımına baş": 4,
    "yeni inşaat": 4,
    "villa projesi": 3,
    "konut projesi": 3,
    "lansman": 2,
    "satışa çıktı": 2,
    "proje": 1,
    "inşaat": 2,
    "müteahhit": 2,
}

NOISE = (
    "kiralık yazlık",
    "günlük kiralık",
    "ikinci el",
    "dekorasyon fikirleri",
    "deprem son dakika",
    "şantiye evleri",
    "şantiye bölgesi",
    "taşınmaya hazır",
    "inşa edilmiş",
    "tamamlanmış",
    "çalışma durdu",
    "durduruldu",
    "iptal edildi",
    "bakanlık freni",
    "davalık",
    "kaçak hafriyat",
    "kaçak inşaat",
    "inşaat yasağı",
)

STRONG_ACTIVE_SIGNALS = (
    "ruhsat", "temel at", "hafriyat", "şantiye kuruldu", "şantiye çalışmaları",
    "inşaata baş", "inşaat baş", "kaba inşaat", "yapımına baş",
)

CANDIDATE_COMPARE_FIELDS = (
    "proje", "firma", "bolge", "sinyal", "notlar",
    "kaynak_tipi", "skor", "durum",
)


def connect():
    return sqlite3.connect(DB, timeout=30)


def _columns(connection, table):
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_missing_columns(connection, table, definitions):
    existing = _columns(connection, table)
    for name, definition in definitions.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def ensure_schema():
    with connect() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS santiyeler (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            durum TEXT DEFAULT 'TURUNCU', mahalle TEXT, ada TEXT, parsel TEXT,
            adres TEXT, enlem REAL, boylam REAL, firma TEXT, proje TEXT,
            neden TEXT, belediye_bilgisi TEXT, internet_bilgisi TEXT,
            harita_bilgisi TEXT, kaynak_url TEXT, son_kontrol TEXT,
            aktif INTEGER DEFAULT 1)"""
        )
        _add_missing_columns(
            connection,
            "santiyeler",
            {
                "durum": "TEXT DEFAULT 'TURUNCU'",
                "mahalle": "TEXT",
                "ada": "TEXT",
                "parsel": "TEXT",
                "adres": "TEXT",
                "enlem": "REAL",
                "boylam": "REAL",
                "firma": "TEXT",
                "proje": "TEXT",
                "neden": "TEXT",
                "belediye_bilgisi": "TEXT",
                "internet_bilgisi": "TEXT",
                "harita_bilgisi": "TEXT",
                "kaynak_url": "TEXT",
                "son_kontrol": "TEXT",
                "aktif": "INTEGER DEFAULT 1",
            },
        )

        connection.execute(
            """CREATE TABLE IF NOT EXISTS internet_adaylari (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            firma TEXT, proje TEXT, bolge TEXT, sinyal TEXT, notlar TEXT,
            kaynak_url TEXT, ilk_gorulme TEXT, son_gorulme TEXT,
            kaynak_tipi TEXT, skor INTEGER DEFAULT 0,
            durum TEXT DEFAULT 'TURUNCU', aktif INTEGER DEFAULT 1)"""
        )
        _add_missing_columns(
            connection,
            "internet_adaylari",
            {
                "firma": "TEXT",
                "proje": "TEXT",
                "bolge": "TEXT",
                "sinyal": "TEXT",
                "notlar": "TEXT",
                "kaynak_url": "TEXT",
                "ilk_gorulme": "TEXT",
                "son_gorulme": "TEXT",
                "kaynak_tipi": "TEXT",
                "skor": "INTEGER DEFAULT 0",
                "durum": "TEXT DEFAULT 'TURUNCU'",
                "aktif": "INTEGER DEFAULT 1",
            },
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS tarama_gecmisi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            baslangic TEXT, bitis TEXT, bulunan INTEGER DEFAULT 0,
            yeni INTEGER DEFAULT 0, guncellenen INTEGER DEFAULT 0,
            hata TEXT)"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_aday_url ON internet_adaylari(kaynak_url)"
        )


def _plain(text):
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _detect_location(text):
    """İlçe adından önce daha spesifik mahalle/bölge eşleşmesini döndürür."""
    combined = _plain(text).lower()
    matches = [
        (len(key), label)
        for key, label in LOCATIONS.items()
        if label != "Çeşme" and key in combined
    ]
    if matches:
        # Birden fazla özel ad geçerse daha uzun/özgül anahtar tercih edilir.
        return max(matches, key=lambda item: item[0])[1]
    if "çeşme" in combined or "cesme" in combined:
        return "Çeşme"
    return None


def _canonical_url(url):
    if not url:
        return ""
    url = unquote(url.strip())
    parsed = urlparse(url)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        url = parse_qs(parsed.query).get("uddg", [url])[0]
        parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""
    clean_query = "&".join(
        part for part in parsed.query.split("&")
        if part and not part.lower().startswith(("utm_", "fbclid=", "gclid="))
    )
    return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), "", clean_query, ""))


def _source_type(url, text=""):
    host = urlparse(url).netloc.lower()
    if "instagram.com" in host or "instagram.com" in (text or "").lower():
        return "Instagram (indekslenmiş)"
    if "cesme.bel.tr" in host:
        return "Çeşme Belediyesi"
    if "google.com" in host or "news.google" in host:
        return "Google Haberler"
    return host.removeprefix("www.") or "Web"


def _evaluate(title, snippet, url, published=None):
    snippet_text = BeautifulSoup(snippet or "", "html.parser").get_text(" ", strip=True)
    combined = _plain(f"{title} {snippet_text} {url}").lower()
    if any(word in combined for word in NOISE):
        return None

    location = _detect_location(combined)
    if not location:
        return None

    matches = []
    score = 2
    for signal, points in SIGNALS.items():
        if signal in combined:
            matches.append(signal)
            score += points
    if not matches:
        return None
    has_strong_signal = any(signal in combined for signal in STRONG_ACTIVE_SIGNALS)
    if "satılık" in combined and "proje" not in combined and not has_strong_signal:
        return None
    if "instagram.com" in url.lower():
        score += 1
    if "cesme.bel.tr" in url.lower():
        score += 2
    if published:
        try:
            published_date = parsedate_to_datetime(published)
            if published_date.tzinfo is None:
                published_date = published_date.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - published_date).days
            # Radarın amacı erken satış sinyali yakalamak. RSS'te eski bir haber
            # tekrar üst sıralara çıktığında bunu yeni/aktif şantiye gibi öne taşıma;
            # kaydı tamamen silmek yerine yaşına göre puanını kontrollü düşür.
            if age_days > 365:
                return None
            if age_days > 180:
                score -= 5
            elif age_days > 90:
                score -= 4
            elif age_days > 30:
                score -= 1
        except (TypeError, ValueError, OverflowError):
            pass
    score = min(score, 10)
    if score < 5:
        return None

    candidate = {
        "proje": _plain(title)[:240] or "Başlıksız bulgu",
        "firma": urlparse(url).netloc.removeprefix("www.")[:120],
        "bolge": location,
        "sinyal": ", ".join(matches[:5]),
        "notlar": _plain(snippet_text)[:700],
        "kaynak_url": _canonical_url(url),
        "kaynak_tipi": _source_type(url, combined),
        "skor": score,
        "durum": "KIRMIZI" if score >= 8 else "TURUNCU",
    }
    return _enrich_candidate(candidate)


def _enrich_candidate(candidate):
    """Teyit edilmiş yüksek değerli projeleri satışa hazır bilgiyle zenginleştirir."""
    project_text = candidate.get("proje", "").lower()
    if "sumen olea" in project_text:
        candidate.update(
            {
                "firma": "SUMEN™ / Sumen Group",
                "proje": "SUMEN Olea — Urla Uzunkuyu yeni konut projesi",
                "bolge": "Uzunkuyu",
                "sinyal": "2026 yeni konut projesi, yapım sürüyor, Aralık 2026 teslim hedefi",
                "notlar": (
                    "Urla Uzunkuyu'da yükselen güncel konut projesi. Satış fiyatları "
                    "11,5–15 milyon TL; teslim hedefi Aralık 2026. Proje iletişim "
                    "telefonu: 0505 388 37 77. Hazır beton ve yapı malzemesi için "
                    "öncelikli saha ve satın alma teyidi önerilir."
                ),
                "kaynak_url": (
                    "https://emlakkulisi.com/sumen-olea-urlada-115-milyon-tlye-"
                    "yeni-proje/827889"
                ),
                "kaynak_tipi": "Doğrulanmış proje haberi",
                "skor": 9,
                "durum": "KIRMIZI",
            }
        )
    return candidate


def _google_news(session, query):
    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=tr&gl=TR&ceid=TR:tr"
    )
    response = session.get(url, timeout=20)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    results = []
    for item in root.findall(".//item")[:20]:
        results.append(
            {
                "title": item.findtext("title") or "",
                "snippet": item.findtext("description") or "",
                "url": item.findtext("link") or "",
                "published": item.findtext("pubDate") or "",
            }
        )
    return results


def _duckduckgo(session, query):
    response = session.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query, "kl": "tr-tr"},
        timeout=25,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results = []
    for result in soup.select(".result")[:20]:
        link = result.select_one(".result__a")
        snippet = result.select_one(".result__snippet")
        if not link:
            continue
        results.append(
            {
                "title": link.get_text(" ", strip=True),
                "snippet": snippet.get_text(" ", strip=True) if snippet else "",
                "url": link.get("href", ""),
            }
        )
    return results


def _stored_candidate_changed(row, item):
    existing = dict(zip(CANDIDATE_COMPARE_FIELDS, row[1:]))
    for field in CANDIDATE_COMPARE_FIELDS:
        old_value = existing.get(field)
        new_value = item.get(field)
        if field == "skor":
            try:
                if int(old_value or 0) != int(new_value or 0):
                    return True
            except (TypeError, ValueError):
                return True
        elif _plain(str(old_value or "")) != _plain(str(new_value or "")):
            return True
    return False


def _store(candidates, deactivate_missing=True):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    new_count = 0
    updated_count = 0
    with connect() as connection:
        if candidates and deactivate_missing:
            # Yalnızca bütün arama kaynakları sağlıklıysa bu turda artık görülmeyen
            # otomatik sonuçları pasife al. Kısmi kaynak hatasında eski bulguyu
            # yanlışlıkla kaybetmek yerine koru.
            connection.execute(
                "UPDATE internet_adaylari SET aktif=0 WHERE kaynak_tipi IS NOT NULL"
            )
        for item in candidates:
            if not item.get("kaynak_url"):
                continue
            row = connection.execute(
                """SELECT id,proje,firma,bolge,sinyal,notlar,kaynak_tipi,skor,durum
                FROM internet_adaylari WHERE kaynak_url=? LIMIT 1""",
                (item["kaynak_url"],),
            ).fetchone()
            if row:
                changed = _stored_candidate_changed(row, item)
                connection.execute(
                    """UPDATE internet_adaylari SET proje=?, firma=?, bolge=?, sinyal=?,
                    notlar=?, son_gorulme=?, kaynak_tipi=?, skor=?, durum=?, aktif=1 WHERE id=?""",
                    (
                        item["proje"], item["firma"], item["bolge"], item["sinyal"],
                        item["notlar"], now, item["kaynak_tipi"], item["skor"],
                        item["durum"], row[0],
                    ),
                )
                if changed:
                    updated_count += 1
            else:
                connection.execute(
                    """INSERT INTO internet_adaylari
                    (firma,proje,bolge,sinyal,notlar,kaynak_url,ilk_gorulme,
                    son_gorulme,kaynak_tipi,skor,durum,aktif)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (
                        item["firma"], item["proje"], item["bolge"], item["sinyal"],
                        item["notlar"], item["kaynak_url"], now, now,
                        item["kaynak_tipi"], item["skor"], item["durum"],
                    ),
                )
                new_count += 1
    return new_count, updated_count


def apply_candidate_retention():
    """Bir tarama kaçırılan adayı korur; iki taramadır görünmeyeni pasife alır.

    Bu kural taramanın içinde çalıştığı için Streamlit'teki manuel tarama ile
    GitHub Actions taraması aynı aday yaşam döngüsünü uygular. Kısmi kaynak
    hatasında hiçbir aday yaşlandırılmaz.
    """
    ensure_schema()
    with connect() as connection:
        scans = connection.execute(
            """SELECT baslangic,hata FROM tarama_gecmisi
            ORDER BY id DESC LIMIT 2"""
        ).fetchall()
        if len(scans) < 2:
            return {"retained": 0, "deactivated": 0, "skipped": True}

        current_started, current_error = scans[0]
        previous_started, _ = scans[1]
        if not current_started or not previous_started:
            return {"retained": 0, "deactivated": 0, "skipped": True}
        if (current_error or "").strip():
            return {"retained": 0, "deactivated": 0, "skipped": True}
        if previous_started >= current_started:
            return {"retained": 0, "deactivated": 0, "skipped": True}

        stale = connection.execute(
            """UPDATE internet_adaylari
            SET aktif=0
            WHERE aktif=1
              AND kaynak_tipi IS NOT NULL
              AND (son_gorulme IS NULL OR son_gorulme<?)""",
            (previous_started,),
        )
        retained = connection.execute(
            """UPDATE internet_adaylari
            SET aktif=1
            WHERE aktif=0
              AND kaynak_tipi IS NOT NULL
              AND son_gorulme IS NOT NULL
              AND son_gorulme>=?
              AND son_gorulme<?""",
            (previous_started, current_started),
        )
        return {
            "retained": max(retained.rowcount or 0, 0),
            "deactivated": max(stale.rowcount or 0, 0),
            "skipped": False,
        }


def scan_and_store(progress_callback=None):
    ensure_schema()
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "tr-TR,tr;q=0.9"})
    found = {}
    errors = []
    total_steps = len(SEARCH_QUERIES) * 2
    completed = 0

    jobs = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for query in SEARCH_QUERIES:
            for engine_name, engine in (("Google Haberler", _google_news), ("Web", _duckduckgo)):
                jobs.append((query, engine_name, pool.submit(engine, session, query)))

        for query, engine_name, future in jobs:
            try:
                for raw in future.result():
                    url = _canonical_url(raw.get("url", ""))
                    candidate = _evaluate(
                        raw.get("title", ""), raw.get("snippet", ""), url,
                        raw.get("published"),
                    )
                    if candidate:
                        key = hashlib.sha1(candidate["kaynak_url"].encode("utf-8")).hexdigest()
                        old = found.get(key)
                        if not old or candidate["skor"] > old["skor"]:
                            found[key] = candidate
            except Exception as exc:
                errors.append(f"{engine_name} / {query}: {type(exc).__name__}")
            completed += 1
            if progress_callback:
                progress_callback(completed / total_steps, f"{query} · {engine_name}")

    # Kısmi arama hatasında bu turda görülmeyen eski adayları pasife alma.
    # Böylece geçici bir motor hatası günlük radar listesini eksiltmez.
    new_count, updated_count = _store(
        list(found.values()),
        deactivate_missing=not errors,
    )
    finished = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with connect() as connection:
        connection.execute(
            """INSERT INTO tarama_gecmisi
            (baslangic,bitis,bulunan,yeni,guncellenen,hata) VALUES(?,?,?,?,?,?)""",
            (started, finished, len(found), new_count, updated_count, "\n".join(errors[:30])),
        )

    retention = apply_candidate_retention()
    return {
        "found": len(found),
        "new": new_count,
        "updated": updated_count,
        "errors": errors,
        "finished": finished,
        "retention": retention,
    }


def _location_self_check():
    assert _detect_location("Ovacık Mahallesi Çeşme yeni inşaat") == "Ovacık"
    assert _detect_location("Boyalık Mahallesi Çeşme villa projesi") == "Boyalık"
    assert _detect_location("16 Eylül Mahallesi Çeşme otel inşaatı") == "16 Eylül"
    assert _detect_location("Urla Uzunkuyu yeni konut projesi") == "Uzunkuyu"
    assert _detect_location("Çeşme yeni inşaat") == "Çeşme"


if __name__ == "__main__":
    _location_self_check()
    result = scan_and_store()
    print(
        f"Tarama tamamlandı: {result['found']} uygun sonuç, "
        f"{result['new']} yeni, {result['updated']} güncellendi."
    )
    if result["errors"]:
        print(f"{len(result['errors'])} kaynak/arama hatası oluştu.")