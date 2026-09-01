"""Sentinel kuru-zemin zaman serisi için aday havuzunu güvenli biçimde genişletir.

Ana ``dry_ground_gap_audit.json`` dosyası günlük raporda küçük ve okunabilir kalmak için
her Sentinel bölgesinden sınırlı sayıda örnek saklar. Ancak zaman-serisi denetiminin yalnız
bu küçük örnek grubunu görmesi, 250-2.000 m² aralığındaki izole saha-benzeri adayların büyük
kısmının geçmiş sahneyle hiç sınanmamasına yol açabilir.

Bu yardımcı, üretim alarm eşiklerini veya görev sayısını değiştirmez. Aynı doğrulanmış
Sentinel çiftini ve aynı kuru-zemin/geometri eşiklerini kullanarak yalnız diagnostik aday
havuzunu en fazla ``TARGET_EXAMPLE_LIMIT`` örneğe çıkarır. Geniş havuz ayrı bir JSON'da
saklanır. Coverage workflow'u ``--apply`` ile ancak sahne kimlikleri ve eşikler hâlâ aynıysa
bu havuzu geçici olarak kalibrasyon seçimine verir; aksi durumda mevcut kanonik rapora
güvenli biçimde geri düşer.
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


def _build_pool() -> int:
    base = _load(SOURCE_REPORT)
    original_limit = int(getattr(gap_audit, "EXAMPLE_LIMIT", 0) or 0)
    gap_audit.EXAMPLE_LIMIT = max(original_limit, TARGET_EXAMPLE_LIMIT)

    try:
        expanded = gap_audit.run_audit()
        compatible, reason = _validate_compatible(base, expanded)
        if not compatible:
            raise RuntimeError(reason)
        expanded = copy.deepcopy(expanded)
        expanded["zaman_serisi_aday_havuzu"] = {
            "durum": "genisletildi",
            "ornek_limiti": TARGET_EXAMPLE_LIMIT,
            "kanonik_aday_sayilari": _pool_counts(base),
            "genis_aday_sayilari": _pool_counts(expanded),
            "not": (
                "Yalnız diagnostik zaman-serisi ve saha kalibrasyonu içindir; "
                "alarm/görev/eşik üretmez."
            ),
        }
        _atomic_write(POOL_REPORT, expanded)
        print(
            "Zaman-serisi aday havuzu genişletildi: "
            f"{_pool_counts(base)} -> {_pool_counts(expanded)}"
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

    print("Temporal aday havuzu öz testi başarılı.")


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
