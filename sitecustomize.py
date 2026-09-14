"""Dar kapsamlı Sentinel hotspot imza uyumluluk köprüsü.

14 Eylül 2026'da ``satellite._hotspots`` tarımsal geniş-yüzey bağlamını
``agricultural_context_mask`` ile taşımaya başladı. Bazı diagnostik ve aday
dengeleme sarmalayıcıları eski imzada kaldığı için yalnız canlı analizde TypeError
oluşabiliyor; ağ kullanmayan self-check bunu yakalamıyor.

Bu dosya geçici ve geri alınabilir bir çalışma-zamanı köprüsüdür. Doğrudan aday
dengeleme, uyarlamalı diyagonal koruma, dedupe-sonrası kota onarımı ve aday kapasite
diagnostiğinde devreye girer. 250 m² ana eşiği, 150–249 m² MİKRO politikası, aday
kotası veya Sentinel spektral eşikleri değişmez. Üretim aday yollarında tarımsal
bağlam orijinal hotspot fonksiyonuna aynen aktarılır. Kapasite diagnostiginde ise eski
yerel sarmalayıcının yalnız aday sayısı/geometri ölçümü korunur; tarımsal bağlamın
üretim adaylarına uygulanması ana analizde değişmeden devam eder.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path


_REBALANCE_TARGET_SCRIPTS = {
    "rebalance_satellite_candidates.py",
    "adaptive_sidecar_guard.py",
    "post_dedupe_construction_quota_runner.py",
}
_CAPACITY_TARGET_SCRIPTS = {
    "candidate_capacity_audit.py",
}
_TARGET_SCRIPTS = _REBALANCE_TARGET_SCRIPTS | _CAPACITY_TARGET_SCRIPTS
_SCRIPT_NAME = Path(sys.argv[0] or "").name


if _SCRIPT_NAME in _TARGET_SCRIPTS:
    import satellite

    _original_analyze_sentinel_change = satellite.analyze_sentinel_change

    if _SCRIPT_NAME in _REBALANCE_TARGET_SCRIPTS:
        import rebalance_satellite_candidates as rebalance

        def _compatible_uncapped_hotspots(
            change_mask,
            bbox,
            pixel_area_m2,
            small_site_mask=None,
            agricultural_context_mask=None,
            limit=satellite.HOTSPOT_LIMIT,
            small_quota=satellite.SMALL_HOTSPOT_QUOTA,
        ):
            """Rebalance dekorasyonunu yeni üretim hotspot imzasıyla çalıştır."""
            del limit, small_quota
            base = rebalance._ORIGINAL_HOTSPOTS(
                change_mask,
                bbox,
                pixel_area_m2,
                small_site_mask=small_site_mask,
                agricultural_context_mask=agricultural_context_mask,
                limit=rebalance.RAW_LIMIT,
                small_quota=0,
            )
            strength_map = rebalance._base_strength_map(
                change_mask,
                bbox,
                pixel_area_m2,
                small_site_mask,
            )
            geometry_map = rebalance._base_geometry_map(
                change_mask,
                bbox,
                pixel_area_m2,
                small_site_mask,
            )
            decorated_base = []
            for item in base:
                updated = dict(item)
                key = rebalance._candidate_key(updated)
                updated[rebalance.STRONG_SIGNAL_FIELD] = strength_map.get(key, 0.0)
                if key in geometry_map:
                    updated.update(geometry_map[key])
                decorated_base.append(updated)

            sidecars = rebalance._diagonal_sidecar_candidates(
                change_mask,
                bbox,
                pixel_area_m2,
                small_site_mask=small_site_mask,
            )
            return decorated_base + sidecars

    def _capacity_signature_adapter(legacy_hotspots):
        """Kapasite/geometri sarmalayıcısına yeni anahtar sözcüğü güvenle yedir."""
        def adapted(
            change_mask,
            bbox,
            pixel_area_m2,
            small_site_mask=None,
            agricultural_context_mask=None,
            limit=satellite.HOTSPOT_LIMIT,
            small_quota=satellite.SMALL_HOTSPOT_QUOTA,
        ):
            del agricultural_context_mask
            return legacy_hotspots(
                change_mask,
                bbox,
                pixel_area_m2,
                small_site_mask=small_site_mask,
                limit=limit,
                small_quota=small_quota,
            )

        return adapted

    def _compat_analyze_sentinel_change(*args, **kwargs):
        current_hotspots = satellite._hotspots
        try:
            parameters = inspect.signature(current_hotspots).parameters
            if "agricultural_context_mask" not in parameters:
                if _SCRIPT_NAME in _REBALANCE_TARGET_SCRIPTS:
                    satellite._hotspots = _compatible_uncapped_hotspots
                else:
                    satellite._hotspots = _capacity_signature_adapter(current_hotspots)
            return _original_analyze_sentinel_change(*args, **kwargs)
        finally:
            satellite._hotspots = current_hotspots

    satellite.analyze_sentinel_change = _compat_analyze_sentinel_change
