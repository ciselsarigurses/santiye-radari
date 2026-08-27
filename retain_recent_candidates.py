"""Arama sıralaması dalgalanmasında aday yaşam döngüsünü dengeler."""

from scanner import connect, ensure_schema


def retain_recent_candidates():
    """Sağlıklı taramada bir kez kaçırılan adayı koru, daha eskisini pasife al.

    scanner._store() sonuç kümesi boş olduğunda eski adayları özellikle pasife
    almıyor. Bu koruma, geçici kaynak sorununda yararlı olsa da tamamen sağlıklı
    ve sıfır sonuçlu bir taramada çok eski kayıtların aktif kalmasına yol
    açabiliyor. Burada son iki başarılı tarama penceresini kullanarak davranışı
    tek kurala indiriyoruz: mevcut veya bir önceki taramada görülen otomatik
    aday aktif kalır; daha eski olan pasife düşer. Kaynak hatası varsa hiçbir
    adayın aktifliğine dokunulmaz.
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

        # Tarama kısmi ise scanner eski adayları zaten korur. Bu durumda burada
        # yaşlandırma yapmak gerçek fırsatı kaynak hatası yüzünden silebilir.
        if (current_error or "").strip():
            return {"retained": 0, "deactivated": 0, "skipped": True}

        # Dakika çözünürlüklü zaman damgaları eşitse güvenli tarafta kal.
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


if __name__ == "__main__":
    result = retain_recent_candidates()
    if result["skipped"]:
        print("Aday kalıcılığı: veri güvenliği nedeniyle bu turda aktiflik değiştirilmedi.")
    else:
        print(
            "Aday kalıcılığı: önceki taramada görülüp bu turda kaybolan "
            f"{result['retained']} kayıt korundu; "
            f"iki taramadır görünmeyen {result['deactivated']} kayıt pasife alındı."
        )
