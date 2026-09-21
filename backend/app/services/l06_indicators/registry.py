"""L6 indicator registry: one class per spectral indicator, all computed on
surface reflectance (0-1) over the shared 10 m grid.

Every indicator is a *satellite proxy*. ``scientific_basis`` states what it proxies
and what confounds it; the UI panel and the PDF brief render that text verbatim,
so it is written for a district engineer, not for a remote-sensing audience.

Reflectance: Sentinel-2 L2A digital numbers are ``DN = (rho + 0.1) * 10000`` for
processing baseline >= 04.00 (every scene since Jan 2022, and the reprocessed
Collection 1 archive). Normalised differences are scale-invariant but *not*
offset-invariant, so the offset must be removed before any index is computed --
otherwise NDTI over water is damped by roughly a factor of three.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np

from app.services.l05_water_detection.mask import normalized_difference

# Sentinel-2 L2A quantisation, processing baseline >= 04.00.
BOA_QUANTIFICATION = 10000.0
BOA_ADD_OFFSET = -1000  # DN units; 0 for baselines < 04.00

# Central wavelengths (nm) used by the FAI baseline interpolation.
WL_RED, WL_NIR, WL_SWIR = 665.0, 842.0, 1610.0

Bands = Mapping[str, np.ndarray]


def to_reflectance(
    bands: Bands,
    *,
    offset: int = BOA_ADD_OFFSET,
    quantification: float = BOA_QUANTIFICATION,
) -> dict[str, np.ndarray]:
    """DN -> bottom-of-atmosphere reflectance as float32 in [0, 1]. Categorical
    bands (SCL) are passed through untouched. Negative reflectance (possible over
    dark water after the offset) is clipped to 0."""
    out: dict[str, np.ndarray] = {}
    for name, arr in bands.items():
        if name == "SCL":
            out[name] = arr
            continue
        rho = (arr.astype(np.float32) + np.float32(offset)) / np.float32(quantification)
        out[name] = np.clip(rho, 0.0, 1.0).astype(np.float32)
    return out


@dataclass(frozen=True)
class IndicatorInfo:
    """Serialisable description of an indicator, for the API and the PDF brief."""

    key: str
    display_name: str
    formula_doc: str
    required_bands: tuple[str, ...]
    valid_range: tuple[float, float]
    scientific_basis: str
    water_only: bool
    units: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "formula_doc": self.formula_doc,
            "required_bands": list(self.required_bands),
            "valid_range": list(self.valid_range),
            "scientific_basis": self.scientific_basis,
            "water_only": self.water_only,
            "units": self.units,
        }


class Indicator(ABC):
    """Base class. Subclasses set the class attributes and implement ``compute``,
    which receives *reflectance* arrays (see ``to_reflectance``) and returns a
    float32 array on the same grid; NaN marks "no value"."""

    key: ClassVar[str]
    display_name: ClassVar[str]
    formula_doc: ClassVar[str]
    required_bands: ClassVar[tuple[str, ...]]
    valid_range: ClassVar[tuple[float, float]]
    scientific_basis: ClassVar[str]
    # True: land pixels are NaN and zonal stats aggregate water pixels only.
    # False: computed over every valid pixel (used by the extent tracker).
    water_only: ClassVar[bool] = True
    units: ClassVar[str] = "index"

    @abstractmethod
    def compute(self, bands: Bands) -> np.ndarray: ...

    def check_bands(self, bands: Bands) -> None:
        missing = [b for b in self.required_bands if b not in bands]
        if missing:
            raise KeyError(f"{self.key}: missing bands {missing}")

    def clip(self, values: np.ndarray) -> tuple[np.ndarray, float]:
        """Clip to ``valid_range``; returns (clipped, fraction of finite pixels clipped)."""
        lo, hi = self.valid_range
        finite = np.isfinite(values)
        n = int(finite.sum())
        if n == 0:
            return values, 0.0
        with np.errstate(invalid="ignore"):
            outside = finite & ((values < lo) | (values > hi))
        clipped = np.where(finite, np.clip(values, lo, hi), np.nan).astype(np.float32)
        return clipped, float(outside.sum()) / n

    @property
    def info(self) -> IndicatorInfo:
        return IndicatorInfo(
            key=self.key,
            display_name=self.display_name,
            formula_doc=self.formula_doc,
            required_bands=self.required_bands,
            valid_range=self.valid_range,
            scientific_basis=self.scientific_basis,
            water_only=self.water_only,
            units=self.units,
        )


class NDTITurbidity(Indicator):
    key = "ndti_turbidity"
    display_name = "Turbidity (NDTI)"
    formula_doc = "NDTI = (B4 - B3) / (B4 + B3)  [red 665 nm, green 560 nm]"
    required_bands = ("B03", "B04")
    valid_range = (-1.0, 1.0)
    scientific_basis = (
        "Red-to-green reflectance ratio; rises with suspended particulate matter "
        "because sediment scatters red light more strongly than clear water does. "
        "Confounded by bottom reflectance in shallow water, sun glint, and high "
        "chlorophyll, which also brightens the red band. Inland water typically sits "
        "between -0.3 and 0.5. A satellite proxy, not a calibrated NTU measurement."
    )

    def compute(self, bands: Bands) -> np.ndarray:
        return normalized_difference(bands["B04"], bands["B03"])


class NDCIChlorophyll(Indicator):
    key = "ndci_chlorophyll"
    display_name = "Chlorophyll-a (NDCI)"
    formula_doc = "NDCI = (B5 - B4) / (B5 + B4)  [red-edge 705 nm, red 665 nm]"
    required_bands = ("B04", "B05")
    valid_range = (-1.0, 1.0)
    scientific_basis = (
        "Red-edge to red reflectance ratio; rises with chlorophyll-a because "
        "phytoplankton absorb red light and reflect at the red edge. Confounded by "
        "high suspended sediment (raises both bands), by shallow-water bottom "
        "reflectance, and by floating vegetation. B5 is a 20 m band resampled to "
        "10 m, so small features are smoothed. A satellite proxy for algal "
        "biomass, not a laboratory chlorophyll concentration."
    )

    def compute(self, bands: Bands) -> np.ndarray:
        return normalized_difference(bands["B05"], bands["B04"])


class FAIAlgal(Indicator):
    key = "fai_algal"
    display_name = "Floating algae (FAI)"
    formula_doc = (
        "FAI = B8 - [B4 + (B11 - B4) * (842 - 665) / (1610 - 665)]  "
        "[NIR minus the red-SWIR baseline at 842 nm]"
    )
    required_bands = ("B04", "B08", "B11")
    valid_range = (-0.2, 0.5)
    scientific_basis = (
        "Height of the near-infrared reflectance above a straight line drawn "
        "between red and shortwave-infrared. Clear water is near zero or slightly "
        "negative; dense surface scums and floating mats push it strongly positive "
        "because they reflect NIR like vegetation. Less sensitive to atmosphere and "
        "sun glint than NDCI, but does not respond to algae suspended below the "
        "surface. Confounded by floating debris, emergent aquatic weeds, and "
        "exposed shoreline mixed into edge pixels. A satellite proxy for surface "
        "bloom cover, not a cell count."
    )

    def compute(self, bands: Bands) -> np.ndarray:
        red = bands["B04"].astype(np.float32)
        nir = bands["B08"].astype(np.float32)
        swir = bands["B11"].astype(np.float32)
        frac = np.float32((WL_NIR - WL_RED) / (WL_SWIR - WL_RED))
        baseline = red + (swir - red) * frac
        return np.asarray(nir - baseline, dtype=np.float32)


class SedimentProxy(Indicator):
    key = "sediment_proxy"
    display_name = "Suspended sediment (red reflectance)"
    formula_doc = "B4 surface reflectance (665 nm), water pixels only"
    required_bands = ("B04",)
    valid_range = (0.0, 0.5)
    units = "reflectance"
    scientific_basis = (
        "Red-band reflectance of water. Clear, deep water absorbs red light and "
        "reflects under about 3 %; suspended mineral sediment scatters it back and "
        "can push reflectance above 10 %. Unlike NDTI it is an absolute brightness, "
        "so it is confounded by residual atmospheric haze, sun glint, whitecaps, "
        "thin cloud missed by the scene classification, and shallow bright bottoms. "
        "A satellite proxy for suspended solids, not a measured mg/L value."
    )

    def compute(self, bands: Bands) -> np.ndarray:
        return np.asarray(bands["B04"], dtype=np.float32).copy()


class MNDWIExtent(Indicator):
    key = "mndwi_extent"
    display_name = "Water extent (MNDWI)"
    formula_doc = "MNDWI = (B3 - B11) / (B3 + B11)  [green 560 nm, SWIR 1610 nm]"
    required_bands = ("B03", "B11")
    valid_range = (-1.0, 1.0)
    water_only = False
    scientific_basis = (
        "Green-to-shortwave-infrared ratio; open water is strongly positive because "
        "it absorbs almost all SWIR, while soil and vegetation are negative. Computed "
        "over the whole zone, not just detected water, so its zonal mean rises and "
        "falls with the share of the zone that is under water and tracks draw-down "
        "and refill. Each observation also carries the zone's visible water "
        "fraction. Confounded by very turbid water (SWIR rises), by wet soil and "
        "shadows (read as water), and by cloud shadow missed by the classification. "
        "A satellite proxy for surface water area; water under cloud is unknown, "
        "not absent."
    )

    def compute(self, bands: Bands) -> np.ndarray:
        return normalized_difference(bands["B03"], bands["B11"])


_ALL: tuple[Indicator, ...] = (
    NDTITurbidity(),
    NDCIChlorophyll(),
    FAIAlgal(),
    SedimentProxy(),
    MNDWIExtent(),
)
INDICATORS: dict[str, Indicator] = {ind.key: ind for ind in _ALL}
INDICATOR_KEYS: tuple[str, ...] = tuple(INDICATORS)
# The four spectral quality indicators of the plan; extent is the fifth, bookkeeping one.
QUALITY_INDICATOR_KEYS: tuple[str, ...] = tuple(k for k in INDICATORS if k != "mndwi_extent")


def get_indicator(key: str) -> Indicator:
    try:
        return INDICATORS[key]
    except KeyError:
        raise KeyError(f"unknown indicator {key!r}; known: {sorted(INDICATORS)}") from None


def describe_indicators() -> list[dict[str, Any]]:
    return [ind.info.as_dict() for ind in INDICATORS.values()]
