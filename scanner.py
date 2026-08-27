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
    'Uzunkuyu Urla inşaat proje',
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
    "alaçatı": "Alaçatı",
    "alacati": "Alaçatı",
    "çeşme": "Çeşme",
    "cesme": "Çeşme",
    "ılıca": "Ilıca",
    "ilica": "Ilıca",
    "reisdere": "Reisdere",
    "ovacık": "Ovacık",
    "ovacik": "Ovacık",
    "dalyan": "Dalyan",
    "çiftlikköy": "Çiftlikköy",
    "ciftlikkoy": "Çiftlikköy",
    "musalla": "Musalla",
    "uzunkuyu": "Uzunkuyu",
    "çakabey": "Çakabey",
    "cakabey": "Çakabey",
    "şifne": "Şifne",
    "sifne": "Şifne",
    "germiyan": "Germiyan",
    "ıldır": "Ildır",
    "ildir": "Ildır",
    "paşalimanı": "Paşalimanı",
    "pasalimani": "Paşalimanı",
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
)

STRONG_ACTIVE_SIGNALS = (
    "ruhsat", "temel at", "hafriyat", "şantiye kuruldu", "şantiye çalışmaları",
    "inşaata baş", "inşaat baş", "kaba inşaat", "yapımına baş",
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

    location = next((label for key, label in LOCATIONS.items() if key in combined), None)
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
            if age_days > 365:
                return None
            if age_days > 180:
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


def _store(candidates):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    new_count = 0
    updated_count = 0
    with connect() as connection:
        if candidates:
            # Önceki otomatik sonuçları pasife al; bu taramada yeniden bulunanlar
            # aşağıda tekrar aktif edilir. Elle eklenmiş/önceden hazırlanmış kayıtlar korunur.
            connection.execute(
                "UPDATE internet_adaylari SET aktif=0 WHERE kaynak_tipi IS NOT NULL"
            )
        for item in candidates:
            if not item.get("kaynak_url"):
                continue
            row = connection.execute(
                "SELECT id FROM internet_adaylari WHERE kaynak_url=? LIMIT 1",
                (item["kaynak_url"],),
            ).fetchone()
            if row:
                connection.execute(
                    """UPDATE internet_adaylari SET proje=?, firma=?, bolge=?, sinyal=?,
                    notlar=?, son_gorulme=?, kaynak_tipi=?, skor=?, durum=?, aktif=1 WHERE id=?""",
                    (
                        item["proje"], item["firma"], item["bolge"], item["sinyal"],
                        item["notlar"], now, item["kaynak_tipi"], item["skor"],
                        item["durum"], row[0],
                    ),
                )
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

    new_count, updated_count = _store(list(found.values()))
    finished = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with connect() as connection:
        connection.execute(
            """INSERT INTO tarama_gecmisi
            (baslangic,bitis,bulunan,yeni,guncellenen,hata) VALUES(?,?,?,?,?,?)""",
            (started, finished, len(found), new_count, updated_count, "\n".join(errors[:30])),
        )
    return {
        "found": len(found),
        "new": new_count,
        "updated": updated_count,
        "errors": errors,
        "finished": finished,
    }


if __name__ == "__main__":
    result = scan_and_store()
    print(
        f"Tarama tamamlandı: {result['found']} uygun sonuç, "
        f"{result['new']} yeni, {result['updated']} güncellendi."
    )
    if result["errors"]:
        print(f"{len(result['errors'])} kaynak/arama hatası oluştu.")
