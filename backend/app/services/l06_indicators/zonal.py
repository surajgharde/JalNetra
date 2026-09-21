"""L6 pixel-to-zone aggregation.

For one scene the whole-body indicator raster is computed once; this module turns
it into one record per (zone, indicator) with mean / p90 / std and the two
quality figures downstream layers filter on:

* ``valid_pixel_pct`` -- share of the zone's pixels that are cloud-free surface
  observations (same definition as the L4 verdict, so a cloudy zone is rejected
  here even when the body as a whole passed).
* ``water_fraction_pct`` -- share of those valid pixels the L5 mask calls water.
  Tracks draw-down per zone and tells the reader how much water the statistic
  actually describes.

A record is rejected (not written) when ``valid_pixel_pct`` is below the
threshold or when too few pixels remain to aggregate; the reason is kept so the
run log can explain the gap in the series.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import numpy as np
from affine import Affine
from shapely.geometry.base import BaseGeometry

from app.services.l04_preprocessing.masks import rasterize_aoi
from app.services.l06_indicators.registry import Indicator

MIN_VALID_PCT = 30.0
MIN_PIXELS = 25  # 0.0025 km2; below this a p90 is noise

REJECT_NO_PIXELS = "zone_off_grid"
REJECT_CLOUDY = "valid_pixel_pct_below_threshold"
REJECT_TOO_FEW = "too_few_pixels"


@dataclass(frozen=True)
class ZoneStats:
    zone_id: str
    indicator: str
    mean: float
    p90: float
    std: float
    n_pixels: int
    valid_pixel_pct: float
    water_fraction_pct: float
    clipped_pct: float


@dataclass(frozen=True)
class ZoneRejection:
    zone_id: str
    indicator: str
    reason: str
    valid_pixel_pct: float
    n_pixels: int


@dataclass(frozen=True)
class ZoneRaster:
    zone_id: str
    mask: np.ndarray  # bool, zone footprint on the grid
    pixels: int
    valid_pixels: int
    water_pixels: int

    @property
    def valid_pixel_pct(self) -> float:
        return 100.0 * self.valid_pixels / self.pixels if self.pixels else 0.0

    @property
    def water_fraction_pct(self) -> float:
        return 100.0 * self.water_pixels / self.valid_pixels if self.valid_pixels else 0.0


def rasterize_zones(
    zones: Iterable[tuple[str, BaseGeometry]],
    crs: str,
    transform: Affine,
    valid: np.ndarray,
    water: np.ndarray,
) -> list[ZoneRaster]:
    """Burn each zone polygon (WGS84) onto the band grid and count its pixels."""
    shape = (int(valid.shape[0]), int(valid.shape[1]))
    out: list[ZoneRaster] = []
    for zone_id, geom in zones:
        m = rasterize_aoi(geom, crs, transform, shape)
        out.append(
            ZoneRaster(
                zone_id=zone_id,
                mask=m,
                pixels=int(m.sum()),
                valid_pixels=int((m & valid).sum()),
                water_pixels=int((m & water).sum()),
            )
        )
    return out


def aggregate_zone(
    zone: ZoneRaster,
    indicator: Indicator,
    values: np.ndarray,
    clipped_flags: np.ndarray,
    *,
    min_valid_pct: float = MIN_VALID_PCT,
    min_pixels: int = MIN_PIXELS,
) -> ZoneStats | ZoneRejection:
    """Aggregate one indicator raster over one zone. ``values`` is the already
    masked (NaN outside water/valid) and clipped raster; ``clipped_flags`` marks
    the pixels that were outside ``valid_range`` before clipping."""
    if zone.pixels == 0:
        return ZoneRejection(zone.zone_id, indicator.key, REJECT_NO_PIXELS, 0.0, 0)
    vpct = round(zone.valid_pixel_pct, 2)
    if vpct < min_valid_pct:
        return ZoneRejection(zone.zone_id, indicator.key, REJECT_CLOUDY, vpct, 0)
    sel = zone.mask & np.isfinite(values)
    n = int(sel.sum())
    if n < min_pixels:
        return ZoneRejection(zone.zone_id, indicator.key, REJECT_TOO_FEW, vpct, n)
    v = values[sel].astype(np.float64)
    return ZoneStats(
        zone_id=zone.zone_id,
        indicator=indicator.key,
        mean=round(float(v.mean()), 5),
        p90=round(float(np.percentile(v, 90)), 5),
        std=round(float(v.std()), 5),
        n_pixels=n,
        valid_pixel_pct=vpct,
        water_fraction_pct=round(zone.water_fraction_pct, 2),
        clipped_pct=round(100.0 * float(clipped_flags[sel].sum()) / n, 2),
    )


def aggregate_all(
    zones: list[ZoneRaster],
    rasters: Mapping[str, tuple[Indicator, np.ndarray, np.ndarray]],
    *,
    min_valid_pct: float = MIN_VALID_PCT,
    min_pixels: int = MIN_PIXELS,
) -> tuple[list[ZoneStats], list[ZoneRejection]]:
    """``rasters``: indicator key -> (indicator, clipped values, clipped flags)."""
    stats: list[ZoneStats] = []
    rejected: list[ZoneRejection] = []
    for zone in zones:
        for ind, values, flags in rasters.values():
            r = aggregate_zone(
                zone, ind, values, flags, min_valid_pct=min_valid_pct, min_pixels=min_pixels
            )
            if isinstance(r, ZoneStats):
                stats.append(r)
            else:
                rejected.append(r)
    return stats, rejected
