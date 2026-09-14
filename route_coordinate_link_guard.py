"""Operasyonel rota Google Maps linkini noktanın kendi koordinatıyla kilitler.

Saha geri bildiriminde eski/yanlış bir Google Maps rota URL'sinin güncel adaydan farklı
hedefe gidebildiği görüldü. Bu koruma yalnız ``operational_route.json`` içindeki
``operasyonel_rota`` satırlarının ``harita`` alanını yeniden üretir. Koordinatı,
görevi, alarmı, Sentinel eşiklerini, sıralamayı veya ada/parsel bilgisini değiştirmez.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROUTE_JSON = Path(__file__).with_name("operational_route.json")


def _point(item):
    try:
        latitude = float(item.get("enlem"))
        longitude = float(item.get("boylam"))
    except (TypeError, ValueError, AttributeError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def canonical_map_link(latitude, longitude):
    return (
        "https://www.google.com/maps/dir/?api=1&destination="
        f"{latitude:.6f},{longitude:.6f}"
    )


def normalize_route(payload):
    if not isinstance(payload, dict):
        return {}, []
    rows = payload.get("operasyonel_rota")
    if not isinstance(rows, list):
        return payload, []

    changed = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        point = _point(item)
        if point is None:
            continue
        expected = canonical_map_link(*point)
        current = str(item.get("harita") or "").strip()
        if current == expected:
            continue
        item["harita"] = expected
        changed.append(
            {
                "gorev_id": str(item.get("gorev_id") or ""),
                "enlem": round(point[0], 6),
                "boylam": round(point[1], 6),
                "eski_harita": current,
                "yeni_harita": expected,
            }
        )
    return payload, changed


def _self_check():
    wrong = (
        "https://www.google.com/maps/dir/38.2893322,26.3867105/"
        "38.262625,26.380094/@38.2644204,26.3781251,16.5z"
    )
    payload = {
        "operasyonel_rota": [
            {
                "gorev_id": "TEST",
                "enlem": 38.271217,
                "boylam": 26.376889,
                "harita": wrong,
            }
        ]
    }
    normalized, changed = normalize_route(payload)
    assert len(changed) == 1
    assert normalized["operasyonel_rota"][0]["harita"] == (
        "https://www.google.com/maps/dir/?api=1&destination=38.271217,26.376889"
    )

    already_ok = {
        "operasyonel_rota": [
            {
                "gorev_id": "OK",
                "enlem": 38.281076,
                "boylam": 26.358687,
                "harita": "https://www.google.com/maps/dir/?api=1&destination=38.281076,26.358687",
            }
        ]
    }
    _, unchanged = normalize_route(already_ok)
    assert unchanged == []

    invalid = {"operasyonel_rota": [{"gorev_id": "BAD", "enlem": 999, "boylam": 26}]}
    normalized, invalid_changed = normalize_route(invalid)
    assert invalid_changed == []
    assert "harita" not in normalized["operasyonel_rota"][0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()

    _self_check()
    if args.self_check:
        print("route coordinate link guard self-check: OK")
        return

    if not ROUTE_JSON.exists():
        print("operational_route.json yok; link koruması uygulanmadı.")
        return

    try:
        payload = json.loads(ROUTE_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        raise SystemExit("operational_route.json okunamadı; dosya değiştirilmedi.")

    payload, changed = normalize_route(payload)
    if not changed:
        print("Operasyonel rota linkleri koordinatlarla zaten tutarlı.")
        return

    ROUTE_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for row in changed:
        print(
            f"{row['gorev_id'] or '-'}: harita hedefi "
            f"{row['enlem']:.6f},{row['boylam']:.6f} olarak düzeltildi."
        )
    print(f"Toplam {len(changed)} operasyonel rota linki düzeltildi.")


if __name__ == "__main__":
    main()
