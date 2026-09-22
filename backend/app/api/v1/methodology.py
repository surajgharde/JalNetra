"""Methodology (read-only): how the platform gets from pixels to a prioritised
alert, with the scientific basis of every indicator.

Everything here is read from the code that does the work - the indicator
registry, the weighted priority model, the settings - so the explanation can
never drift from the implementation. The dashboard renders it as the
Methodology tab; the same JSON is what a reviewer can cite.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.services.l04_preprocessing.masks import SCL_CLOUD_CLASSES, SCL_NODATA_CLASSES
from app.services.l06_indicators.registry import INDICATORS
from app.services.l09_fusion.models import weights_document
from app.services.l10_explain.explain import DISCLAIMER

router = APIRouter(prefix="/methodology", tags=["methodology"])


class IndicatorDoc(BaseModel):
    key: str
    display_name: str
    formula: str
    required_bands: list[str]
    valid_range: list[float]
    units: str
    water_only: bool
    scientific_basis: str
    observes: str = Field(description="Which problem-statement indicator this covers")


class Step(BaseModel):
    key: str
    title: str
    summary: str
    details: list[str]
    parameters: dict[str, Any] = Field(default_factory=dict)


class Methodology(BaseModel):
    workflow: list[str]
    product_boundary: str
    disclaimer: str
    data: Step
    water_detection: Step
    indicators: list[IndicatorDoc]
    temporal_monitoring: Step
    anomaly_detection: Step
    prioritisation: Step
    alerts: Step
    explainability: Step
    validation_loop: Step
    limitations: list[str]


OBSERVES = {
    "ndti_turbidity": "Turbidity",
    "ndci_chlorophyll": "Chlorophyll-related indicators",
    "fai_algal": "Algal activity (surface blooms and scums)",
    "sediment_proxy": "Suspended sediment",
    "mndwi_extent": "Water extent and shoreline change",
}


@router.get("", response_model=Methodology)
async def methodology(settings: Annotated[Settings, Depends(get_settings)]) -> Methodology:
    """The end-to-end method, with the scientific basis and formula of each indicator."""
    weights = weights_document()
    return Methodology(
        workflow=["Monitoring", "Detection", "Prioritisation", "Investigation support"],
        product_boundary=(
            "The platform monitors optically observable indicators and flags anomalies "
            "for ground investigation. It does not measure concentrations, does not "
            "attribute cause, and does not replace laboratory testing."
        ),
        disclaimer=DISCLAIMER,
        data=Step(
            key="data",
            title="Satellite data",
            summary=(
                "Copernicus Sentinel-2 Level-2A surface reflectance (10-20 m, ~5-day revisit) "
                "from AWS Earth Search, Copernicus CDSE, or Google Earth Engine, with automatic "
                "fallback between sources."
            ),
            details=[
                "Bands B03 green, B04 red, B05 red-edge, B08 NIR, B11 SWIR and the Scene "
                "Classification Layer (SCL) are read for the water body's bounding box only "
                "(windowed HTTP range reads of cloud-optimised GeoTIFFs, or Earth Engine "
                "computePixels on the identical 10 m grid).",
                "ESA's reflectance add-offset is applied per scene from the product metadata, "
                "so indicators are comparable across processing baselines and sources.",
                "Every band array is cached once per (water body, scene); the pipeline never "
                "re-downloads and every stage is idempotent and resumable.",
            ],
            parameters={
                "collection": "Sentinel-2 L2A",
                "grid_m": 10,
                "scene_cloud_threshold_pct": settings.stac_max_cloud_pct,
                "sources": ["earth-search", "cdse", "gee" if settings.gee_enabled else None],
            },
        ),
        water_detection=Step(
            key="water_detection",
            title="Water-body detection",
            summary=(
                "Per scene: mask cloud, cloud shadow, cirrus and snow from the SCL, then "
                "threshold MNDWI with Otsu's method inside the registered outline (+60 m) so "
                "the detected shoreline follows draw-down, refill and flooding."
            ),
            details=[
                f"SCL classes {sorted(SCL_CLOUD_CLASSES)} (cloud shadow, cloud medium/high, "
                f"cirrus, snow) are masked and dilated; classes {sorted(SCL_NODATA_CLASSES)} "
                "are no-data. A scene with too little valid area over the body is rejected, "
                "not guessed.",
                "MNDWI = (green - SWIR) / (green + SWIR) separates water from land and wet "
                "soil; Otsu's threshold is fitted per scene on the valid pixels and only "
                "trusted inside a plausible band, otherwise a fixed fallback is used and the "
                "choice is recorded.",
                "Morphological opening/closing and a minimum component size remove speckle; "
                "the result is intersected with the registered polygon buffered by 60 m so "
                "a flooded field next to the lake is not counted as the lake.",
                "Each observation stores water extent (km2), water fraction and valid-pixel "
                "share, so a smaller mask under cloud is reported as unknown, not as loss.",
            ],
            parameters={
                "min_valid_pct": settings.mask_min_valid_pct,
                "outline_buffer_m": 60,
                "threshold": "Otsu on MNDWI, per scene, with audited fallback",
            },
        ),
        indicators=[
            IndicatorDoc(
                key=ind.key,
                display_name=ind.display_name,
                formula=ind.formula_doc,
                required_bands=list(ind.required_bands),
                valid_range=list(ind.valid_range),
                units=ind.units,
                water_only=ind.water_only,
                scientific_basis=ind.scientific_basis,
                observes=OBSERVES.get(ind.key, ""),
            )
            for ind in INDICATORS.values()
        ],
        temporal_monitoring=Step(
            key="temporal",
            title="Temporal monitoring",
            summary=(
                "Every indicator is aggregated per zone per pass and compared with that "
                "zone's own seasonal baseline: a robust day-of-year window over multi-year "
                "history, so a monsoon value is judged against monsoon history."
            ),
            details=[
                f"Baseline window: +/- {settings.baseline_window_days // 2} days of day-of-year "
                f"across all available years; median and MAD-based sigma; a window with fewer "
                f"than {settings.baseline_min_samples} samples is 'building' and cannot alert.",
                f"Tier 1 bodies need {settings.baseline_min_history_days_tier1} days of history "
                f"before baselines are usable, others {settings.baseline_min_history_days}.",
                "Reference-vs-current image pairs and the full series with the seasonal "
                "p10-p90 band are shown on the dashboard and in every brief.",
                "Daily rainfall (Open-Meteo ERA5 archive + forecast) is stored per body as a "
                "covariate for the rainfall gate.",
            ],
        ),
        anomaly_detection=Step(
            key="anomaly",
            title="Intelligent anomaly detection",
            summary=(
                "Three independent detectors vote per zone and pass; a rainfall gate then "
                "decides whether a deviation is more plausibly natural runoff."
            ),
            details=[
                f"Temporal: robust z-score of each indicator against its seasonal baseline; "
                f"|z| >= {settings.anomaly_z_threshold} flags, |z| >= {settings.anomaly_z_high} "
                "on its own already means 'high'. A per-indicator sigma floor stops a nearly "
                "constant history from turning sensor noise into a huge z.",
                f"Spatial: within-scene per-pixel z over water, DBSCAN clustering of hot pixels "
                f"(eps {settings.spatial_eps_px} px, min {settings.spatial_min_samples}); a "
                f"compact cluster >= {settings.spatial_min_area_km2} km2 is a plume or patch, "
                f"while > {settings.spatial_max_hot_fraction:.0%} hot water is a body-wide "
                "shift, not a plume.",
                f"Multivariate: IsolationForest over all indicators plus water-extent change, "
                f"fitted on the zone's history (min {settings.multivariate_min_history} scenes, "
                f"contamination {settings.multivariate_contamination}).",
                "Severity from votes: 3 -> high; 2 -> high if max |z| is extreme else medium; "
                "1 -> medium if extreme else low.",
                f"Rainfall gate: when 72 h rain exceeds the day-of-year p90 (window "
                f"{settings.rainfall_gate_window_days} days) the candidate is marked 'natural "
                "cause likely' and its severity is capped, never hidden.",
            ],
        ),
        prioritisation=Step(
            key="prioritisation",
            title="Prioritisation (0-100)",
            summary=(
                "A transparent weighted model turns the detector outputs into a priority "
                "score for the investigation queue; a learned model can replace it once "
                "enough field validations exist."
            ),
            details=[
                weights["note"],
                "Weights (points at full saturation): "
                + ", ".join(f"{k} {v:+.0f}" for k, v in weights["weights"].items()),
                f"A temporal z of {settings.priority_z_saturation:.0f} counts as a full "
                "deviation; rainfall above the seasonal median subtracts points.",
                "Confidence is separate from severity: it reflects cloud-free share, baseline "
                "maturity and detector agreement, so a cloudy scene lowers confidence, not "
                "priority.",
                f"An XGBoost regressor is trained only once >= "
                f"{settings.priority_train_min_validations} validated alerts exist; its SHAP "
                "values feed the same explanation contract.",
            ],
            parameters={"model_version": weights["version"]},
        ),
        alerts=Step(
            key="alerts",
            title="Alert generation",
            summary=(
                "An alertable candidate becomes an alert carrying location, date/time, the "
                "affected region, the primary indicator, severity and confidence, and evidence."
            ),
            details=[
                "Location: water body, zone and centroid; affected region: the flagged "
                "cluster polygon (GeoJSON) and its area, or the whole zone.",
                "Evidence: current and reference chips, anomaly mask, the scene ids, the "
                "indicator readings against baseline, and a one-page PDF brief.",
                f"Open alerts for the same zone and indicator absorb new observations for "
                f"{settings.alert_dedupe_days} days instead of duplicating; the timeline keeps "
                "every pass.",
                "Delivery by webhook and e-mail to jurisdiction-matched recipients; a master "
                "switch keeps a dev box from paging anyone.",
            ],
        ),
        explainability=Step(
            key="explainability",
            title="Explainability",
            summary=(
                "Every alert states why in plain words and in numbers: a summary sentence, "
                "the indicator deviations, and four grouped contributions that sum to the score."
            ),
            details=[
                "Contributions: indicator deviation, spatial extent, detector agreement, and "
                "context (rainfall, cloud), each with its raw inputs.",
                "The summary is boundary-checked: it names an observation and a location, "
                "never a cause or a source, and always ends with the disclaimer.",
            ],
        ),
        validation_loop=Step(
            key="validation",
            title="Investigation support and validation",
            summary=(
                "Field teams record samples and lab results against an alert; the platform "
                "issues a matched / not-matched / inconclusive verdict and learns from it."
            ),
            details=[
                f"A sample taken more than {settings.validation_max_lag_days} days after the "
                "observation is inconclusive by rule.",
                "Verdicts feed the baseline sample store and, in volume, the learned priority "
                "model, so false positives cost future priority.",
            ],
            parameters={"thresholds": settings.validation_thresholds},
        ),
        limitations=[
            "Optical only: nothing is observed under cloud, at night, or below the surface "
            "beyond a few metres; dissolved contaminants with no optical signature are invisible.",
            "Indicators are proxies, not concentrations; NDTI is not NTU and NDCI is not ug/L.",
            "Shallow bright bottoms, sun glint, whitecaps and terrain shadow can bias "
            "indicators; the SCL and the registered outline limit but do not remove this.",
            "Baselines need multi-year history; a newly registered body reports 'baseline "
            "building' and does not alert until the seasonal band is usable.",
            "10-20 m pixels: narrow river stretches and small ponds carry few pixels and "
            "wider error bars.",
        ],
    )
