"""Arama sıralaması dalgalanmasında aday yaşam döngüsünü dengeler."""

from scanner import apply_candidate_retention


def retain_recent_candidates():
    """Scanner ile aynı tek-tur tolerans kuralını uygula.

    Asıl yaşam döngüsü artık ``scan_and_store`` içinde çalışır; bu küçük sarmalayıcı
    mevcut GitHub Actions adımını geriye dönük uyum için korur. İkinci kez
    çalıştırılması güvenlidir ve aynı kuralın farklılaşmasını önler.
    """
    return apply_candidate_retention()


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
