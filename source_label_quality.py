import sqlite3
from pathlib import Path
from urllib.parse import urlparse


DB = Path(__file__).with_name("santiye.db")
AUTO_LABELS = {
    "Instagram (indekslenmiş)",
    "Google Haberler",
    "Çeşme Belediyesi",
}


def expected_auto_label(url, current_label):
    """Yalnız otomatik kaynak etiketlerini URL hostuna göre düzelt.

    Başlık/snippet içinde geçen `instagram.com` gibi metinler kaynak türünü
    belirlemez. Böylece Google News RSS sonucu, Instagram aramasından gelmiş olsa
    bile yanlışlıkla Instagram diye raporlanmaz. Elle doğrulanmış özel etiketler
    korunur.
    """
    if current_label not in AUTO_LABELS:
        return current_label

    host = urlparse((url or "").strip()).netloc.lower()
    if "cesme.bel.tr" in host:
        return "Çeşme Belediyesi"
    if "news.google" in host or "google.com" in host:
        return "Google Haberler"
    if "instagram.com" in host:
        return "Instagram (indekslenmiş)"
    return current_label


def _self_test():
    assert expected_auto_label(
        "https://news.google.com/rss/articles/test",
        "Instagram (indekslenmiş)",
    ) == "Google Haberler"
    assert expected_auto_label(
        "https://www.instagram.com/p/test/",
        "Google Haberler",
    ) == "Instagram (indekslenmiş)"
    assert expected_auto_label(
        "https://www.cesme.bel.tr/haber/test",
        "Instagram (indekslenmiş)",
    ) == "Çeşme Belediyesi"
    assert expected_auto_label(
        "https://emlakkulisi.com/proje/test",
        "Doğrulanmış proje haberi",
    ) == "Doğrulanmış proje haberi"


def repair_database():
    if not DB.exists():
        return 0

    with sqlite3.connect(DB, timeout=30) as connection:
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='internet_adaylari'"
        ).fetchone()
        if not table_exists:
            return 0

        rows = connection.execute(
            "SELECT id, kaynak_url, kaynak_tipi FROM internet_adaylari "
            "WHERE kaynak_tipi IN (?,?,?)",
            tuple(AUTO_LABELS),
        ).fetchall()

        fixed = 0
        for row_id, url, current_label in rows:
            expected = expected_auto_label(url, current_label)
            if expected == current_label:
                continue
            connection.execute(
                "UPDATE internet_adaylari SET kaynak_tipi=? WHERE id=?",
                (expected, row_id),
            )
            fixed += 1
        return fixed


if __name__ == "__main__":
    _self_test()
    fixed = repair_database()
    print(f"Kaynak etiketi kalite kontrolü tamamlandı: {fixed} kayıt düzeltildi.")
