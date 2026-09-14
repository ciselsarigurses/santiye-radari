"""Dar kapsamlı Sentinel hotspot imza uyumluluk köprüsü.

14 Eylül 2026'da ``satellite._hotspots`` tarımsal geniş-yüzey bağlamını
``agricultural_context_mask`` ile taşımaya başladı. ``rebalance_satellite_candidates``
içindeki sınırsız aday sarmalayıcısı eski imzada kaldığı için yalnız canlı analizde
TypeError oluştu; ağ kullanmayan self-check bunu yakalamadı.

Bu dosya geçici ve geri alınabilir bir çalışma-zamanı köprüsüdür. Yalnız doğrudan
aday dengeleme ve uyarlamalı diyagonal koruma komutlarında devreye girer. 250 m² ana
eşiği, 150–249 m² MİKRO politikası, aday kotası veya Sentinel spektral eşikleri
değişmez. Tarımsal bağlamı düşürmek yerine orijinal üretim hotspot fonksiyonuna aynen
aktarır; böylece tarla/geniş homojen yüzey bastırması korunur.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path


_TARGET_SCRIPTS = {
    "rebalance_satellite_candidates.py",
    "adaptive_sidecar_guard.py",
}


if Path(sys.argv[0] or "").name in _TARGET_SCRIPTS:
    import rebalance_satellite_candidates as rebalance
    import satellite

    _original_analyze_sentinel_change = satellite.analyze_sentinel_change

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

    def _compat_analyze_sentinel_change(*args, **kwargs):
        current_hotspots = satellite._hotspots
        try:
            parameters = inspect.signature(current_hotspots).parameters
            if "agricultural_context_mask" not in parameters:
                satellite._hotspots = _compatible_uncapped_hotspots
            return _original_analyze_sentinel_change(*args, **kwargs)
        finally:
            satellite._hotspots = current_hotspots

    satellite.analyze_sentinel_change = _compat_analyze_sentinel_change
