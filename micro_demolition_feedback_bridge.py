"""MİKRO saha kalibrasyonunda doğrulanmış yıkım/temizliği fiziksel müdahale olarak korur.

Kullanıcı saha geri bildiriminde yıkım/parsel temizliğinin genellikle yeni inşaatın
başlangıcı olduğunu netleştirdi. Ana MİKRO katman yine 150-249 m² diagnostiktir ve
alarm/saha görevi üretmez. Bu köprü yalnız mevcut ``micro_site_field_guard`` içindeki
saha sonucu okuma kümesine ``YIKIM_TEMIZLIK`` sınıfını ekler.

Sonuç olarak aynı Sentinel sahnesinde yakın bir doğrulanmış yıkım noktası yeni bir
MİKRO fırsat gibi yeniden gösterilmez; kayıt bilinen fiziksel müdahale olarak arka
plana alınır. Daha yeni Sentinel sahnesi geldiğinde eski yıkım sonucu veto olmaz ve
nokta yeniden değerlendirilebilir. 250 m² ana üretim eşiğine dokunulmaz.
"""

from __future__ import annotations

import micro_site_field_guard as base

DEMOLITION_OUTCOME = "YIKIM_TEMIZLIK"


def enable_demolition_feedback() -> None:
    base.KNOWN_OUTCOMES.add(DEMOLITION_OUTCOME)


def _self_check() -> None:
    original = set(base.KNOWN_OUTCOMES)
    try:
        enable_demolition_feedback()
        assert DEMOLITION_OUTCOME in base.KNOWN_OUTCOMES

        shortlist = {
            "ana_uretim_esigi_m2": 250,
            "mikro_aralik_m2": [150, 249],
            "kisa_liste": [
                {
                    "bolge": "uzunkuyu",
                    "enlem": 38.33155,
                    "boylam": 26.64434,
                    "alan_m2": 200,
                }
            ],
        }
        demolition = [
            {
                "gorev_id": "UTESTYIKIM",
                "sonuc": DEMOLITION_OUTCOME,
                "enlem": 38.331547,
                "boylam": 26.644338,
                "son_tarih": "08.09.2026",
                "kayit_zamani": "2026-09-10 17:06 UTC",
            }
        ]

        same_scene = base.build_review(
            shortlist,
            {"bolgeler": {"uzunkuyu": {"son_tarih": "08.09.2026"}}},
            demolition,
        )
        assert same_scene["ana_uretim_esigi_m2"] == 250
        assert same_scene["mikro_aralik_m2"] == [150, 249]
        assert same_scene["alarm"] is False
        assert same_scene["saha_gorevi"] is False
        assert same_scene["guncel_saha_eslesmesi_arka_plan"] == 1
        assert same_scene["yeni_mikro_inceleme_adayi"] == 0
        assert (
            same_scene["arka_plan_saha_eslesmeleri"][0]["saha_eslesme_sonucu"]
            == DEMOLITION_OUTCOME
        )

        newer_scene = base.build_review(
            shortlist,
            {"bolgeler": {"uzunkuyu": {"son_tarih": "11.09.2026"}}},
            demolition,
        )
        assert newer_scene["guncel_saha_eslesmesi_arka_plan"] == 0
        assert newer_scene["yeni_mikro_inceleme_adayi"] == 1
    finally:
        base.KNOWN_OUTCOMES.clear()
        base.KNOWN_OUTCOMES.update(original)


def main() -> None:
    _self_check()
    enable_demolition_feedback()
    base.main()


if __name__ == "__main__":
    main()
