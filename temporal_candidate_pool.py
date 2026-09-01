"""Sentinel kuru-zemin zaman serisi için aday havuzunu güvenli biçimde genişletir.

Ana ``dry_ground_gap_audit.json`` dosyası günlük raporda küçük ve okunabilir kalmak için
her Sentinel bölgesinden sınırlı sayıda örnek saklar. Ancak zaman-serisi denetiminin yalnız
bu küçük örnek grubunu görmesi, 250-2.000 m² aralığındaki izole saha-benzeri adayların büyük
kısmının geçmiş sahneyle hiç sınanmamasına yol açabilir.

Bu yardımcı, üretim alarm eşiklerini veya görev sayısını değiştirmez. Aynı doğrulanmış
Sentinel çiftini ve aynı kuru-zemin/geometri eşiklerini kullanarak daha geniş bir diagnostik
aday grubunu tarar; en güçlü adayların çekirdeğini korurken kalan zaman-serisi kapasitesini
mümkün olduğunda farklı mahallelere yayar. Geniş havuz ayrı bir JSON'da saklanır. Coverage
workflow'u ``--apply`` ile ancak sahne kimlikleri ve eşikler hâlâ aynıysa bu havuzu geçici
olarak kalibrasyon seçimine verir; aksi durumda mevcut kanonik rapora güvenli biçimde geri
düşer.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
from pathlib import Path

import dry_ground_gap_audit as gap_audit


SOURCE_REPORT = Path(__file__).with_name("dry_ground_gap_audit.json")
POOL_REPORT = Path(__file__).with_name("dry_ground_temporal_pool.json")
TARGET_EXAMPLE_LIMIT = 24
SCAN_EXAMPLE_LIMIT = 96
CORE_STRENGTH_LIMIT = 12
PAIR_KEYS = ("onceki_item", "son_item")


def _load(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} JSON nesnesi değil")
    return data


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _candidate_count(region: dict) -> int:
    rows = region.get("saha_benzeri_ornekler") or []
    return len(rows) if isinstance(rows, list) else 0


def _neighborhood(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _candidate_key(item: dict) -> tuple[object, object, object]:
    return (item.get("enlem"), item.get("boylam"), item.get("alan_m2"))


def _diverse_rows(
    rows: list[dict],
    *,
    limit: int = TARGET_EXAMPLE_LIMIT,
    core_limit: int = CORE_STRENGTH_LIMIT,
) -> list[dict]:
    """Güç sırasının çekirdeğini korur, ek kapasiteyi farklı mahallelere yayar.

    ``dry_ground_gap_audit`` satırları izolasyon ve spektral güç sırasındadır. İlk çekirdek
    aynen korunur; sonraki slotlarda önce henüz temsil edilmeyen mahallelerin en güçlü adayı
    alınır. Slot kalırsa özgün güç sırasıyla doldurulur. Böylece güçlü Alaçatı/Ovacık kümeleri
    kaybolmadan yarımadanın daha az temsil edilen mahalleleri de temporal teste girebilir.
    """
    clean_rows = [item for item in rows if isinstance(item, dict)]
    if limit <= 0:
        return []
    if len(clean_rows) <= limit:
        return clean_rows

    core_size = min(max(int(core_limit), 0), int(limit), len(clean_rows))
    selected = list(clean_rows[:core_size])
    selected_keys = {_candidate_key(item) for item in selected}
    seen_neighborhoods = {
        _neighborhood(item.get("mahalle"))
        for item in selected
        if _neighborhood(item.get("mahalle"))
    }

    for item in clean_rows[core_size:]:
        if len(selected) >= limit:
            break
        neighborhood = _neighborhood(item.get("mahalle"))
        if not neighborhood or neighborhood in seen_neighborhoods:
            continue
        key = _candidate_key(item)
        if key in selected_keys:
            continue
        selected.append(item)
        selected_keys.add(key)
        seen_neighborhoods.add(neighborhood)

    if len(selected) < limit:
        for item in clean_rows[core_size:]:
            if len(selected) >= limit:
                break
            key = _candidate_key(item)
            if key in selected_keys:
                continue
            selected.append(item)
            selected_keys.add(key)

    return selected


def _diversify_report(report: dict) -> dict:
    diversified = copy.deepcopy(report)
    for region in (diversified.get("bolgeler") or {}).values():
        if not isinstance(region, dict) or region.get("durum") != "ok":
            continue
        rows = region.get("saha_benzeri_ornekler") or []
        if isinstance(rows, list):
            region["saha_benzeri_ornekler"] = _diverse_rows(rows)
        raw_examples = region.get("ornekler") or []
        if isinstance(raw_examples, list) and len(raw_examples) > TARGET_EXAMPLE_LIMIT:
            region["ornekler"] = raw_examples[:TARGET_EXAMPLE_LIMIT]
    return diversified


def _validate_compatible(base: dict, candidate: dict) -> tuple[bool, str]:
    if base.get("esikler") != candidate.get("esikler"):
        return False, "kuru-zemin eşikleri değişmiş"

    base_regions = base.get("bolgeler") or {}
    candidate_regions = candidate.get("bolgeler") or {}
    if not isinstance(base_regions, dict) or not base_regions:
        return False, "kanonik raporda bölge yok"

    for region_key, base_region in base_regions.items():
        candidate_region = candidate_regions.get(region_key)
        if not isinstance(candidate_region, dict):
            return False, f"{region_key}: geniş havuz bölgesi yok"
        if base_region.get("durum") != "ok" or candidate_region.get("durum") != "ok":
            return False, f"{region_key}: Sentinel çifti hazır değil"
        for key in PAIR_KEYS:
            base_value = str(base_region.get(key) or "")
            candidate_value = str(candidate_region.get(key) or "")
            if not base_value or base_value != candidate_value:
                return False, f"{region_key}: {key} eşleşmiyor"
        if _candidate_count(candidate_region) < _candidate_count(base_region):
            return False, f"{region_key}: aday sayısı kanonik rapordan azalmış"

    return True, "ok"


def _pool_counts(report: dict) -> dict[str, int]:
    return {
        key: _candidate_count(value)
        for key, value in (report.get("bolgeler") or {}).items()
        if isinstance(value, dict)
    }


def _neighborhood_counts(report: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key, region in (report.get("bolgeler") or {}).items():
        if not isinstance(region, dict):
            continue
        neighborhoods = {
            _neighborhood(item.get("mahalle"))
            for item in (region.get("saha_benzeri_ornekler") or [])
            if isinstance(item, dict) and _neighborhood(item.get("mahalle"))
        }
        counts[str(key)] = len(neighborhoods)
    return counts


def _build_pool() -> int:
    base = _load(SOURCE_REPORT)
    original_limit = int(getattr(gap_audit, "EXAMPLE_LIMIT", 0) or 0)
    gap_audit.EXAMPLE_LIMIT = max(original_limit, SCAN_EXAMPLE_LIMIT)

    try:
        scanned = gap_audit.run_audit()
        compatible, reason = _validate_compatible(base, scanned)
        if not compatible:
            raise RuntimeError(reason)
        expanded = _diversify_report(scanned)
        compatible, reason = _validate_compatible(base, expanded)
        if not compatible:
            raise RuntimeError(reason)
        expanded["zaman_serisi_aday_havuzu"] = {
            "durum": "genisletildi_ve_mahalle_dengeli",
            "tarama_limiti": SCAN_EXAMPLE_LIMIT,
            "ornek_limiti": TARGET_EXAMPLE_LIMIT,
            "guclu_cekirdek": CORE_STRENGTH_LIMIT,
            "kanonik_aday_sayilari": _pool_counts(base),
            "genis_aday_sayilari": _pool_counts(expanded),
            "temsil_edilen_mahalle_sayilari": _neighborhood_counts(expanded),
            "not": (
                "Yalnız diagnostik zaman-serisi ve saha kalibrasyonu içindir; "
                "alarm/görev/eşik üretmez. En güçlü çekirdek korunur, kalan slotlar "
                "mümkün olduğunda farklı mahallelere yayılır."
            ),
        }
        _atomic_write(POOL_REPORT, expanded)
        print(
            "Zaman-serisi aday havuzu mahalle kapsamasıyla genişletildi: "
            f"{_pool_counts(base)} -> {_pool_counts(expanded)}, "
            f"mahalle={_neighborhood_counts(expanded)}"
        )
    except Exception as exc:
        # Ek kapsama katmanı ana zaman-serisi denetimini kırmasın. Eski/stale havuzu
        # asla bırakma; aynı günün kanonik 8-aday raporunu güvenli fallback olarak yaz.
        _atomic_write(POOL_REPORT, base)
        print(
            "Zaman-serisi aday havuzu genişletilemedi; kanonik havuz korunuyor: "
            f"{type(exc).__name__}: {exc}"
        )
    finally:
        gap_audit.EXAMPLE_LIMIT = original_limit

    return 0


def _apply_pool() -> int:
    if not POOL_REPORT.exists():
        print("Geniş zaman-serisi havuzu yok; kanonik kuru-zemin raporu korunuyor.")
        return 0

    base = _load(SOURCE_REPORT)
    pool = _load(POOL_REPORT)
    compatible, reason = _validate_compatible(base, pool)
    if not compatible:
        print(f"Geniş havuz uygulanmadı: {reason}. Kanonik rapor korunuyor.")
        return 0

    _atomic_write(SOURCE_REPORT, pool)
    print(f"Geniş zaman-serisi havuzu geçici olarak uygulandı: {_pool_counts(pool)}")
    return 0


def _self_check() -> None:
    base = {
        "esikler": {"alan_m2": [250, 2000]},
        "bolgeler": {
            "cesme": {
                "durum": "ok",
                "onceki_item": "old-a",
                "son_item": "new-a",
                "saha_benzeri_ornekler": [{"id": 1}],
            },
            "uzunkuyu": {
                "durum": "ok",
                "onceki_item": "old-b",
                "son_item": "new-b",
                "saha_benzeri_ornekler": [{"id": 2}],
            },
        },
    }
    expanded = copy.deepcopy(base)
    expanded["bolgeler"]["cesme"]["saha_benzeri_ornekler"].append({"id": 3})
    ok, reason = _validate_compatible(base, expanded)
    assert ok, reason

    wrong_scene = copy.deepcopy(expanded)
    wrong_scene["bolgeler"]["cesme"]["son_item"] = "newer-a"
    ok, _ = _validate_compatible(base, wrong_scene)
    assert not ok

    wrong_threshold = copy.deepcopy(expanded)
    wrong_threshold["esikler"]["alan_m2"] = [200, 2000]
    ok, _ = _validate_compatible(base, wrong_threshold)
    assert not ok

    fewer = copy.deepcopy(base)
    fewer["bolgeler"]["cesme"]["saha_benzeri_ornekler"] = []
    ok, _ = _validate_compatible(base, fewer)
    assert not ok

    sample_rows = [
        {"enlem": 38.10 + index / 1000, "boylam": 26.30, "alan_m2": 300, "mahalle": "Alaçatı"}
        for index in range(6)
    ]
    sample_rows.extend(
        [
            {"enlem": 38.20, "boylam": 26.40, "alan_m2": 400, "mahalle": "Ovacık"},
            {"enlem": 38.30, "boylam": 26.50, "alan_m2": 500, "mahalle": "Germiyan"},
            {"enlem": 38.40, "boylam": 26.60, "alan_m2": 600, "mahalle": "Ildır"},
        ]
    )
    diversified = _diverse_rows(sample_rows, limit=6, core_limit=3)
    assert diversified[:3] == sample_rows[:3], "Güçlü çekirdek sırası bozuldu."
    assert {_neighborhood(item["mahalle"]) for item in diversified} >= {
        "alaçatı",
        "ovacık",
        "germiyan",
        "ıldır",
    }, diversified
    assert len({_candidate_key(item) for item in diversified}) == len(diversified)

    print("Temporal aday havuzu öz testi başarılı; güçlü çekirdek korunup mahalle kapsaması genişliyor.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if args.check_only:
        _self_check()
        return 0
    if args.apply:
        return _apply_pool()
    return _build_pool()


if __name__ == "__main__":
    raise SystemExit(main())