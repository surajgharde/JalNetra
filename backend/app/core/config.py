"""Application settings, read from environment / .env via pydantic-settings."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    app_name: str = "JalNetra"
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # --- Database ---
    database_url: str = "postgresql+asyncpg://jalnetra:jalnetra@localhost:5432/jalnetra"

    # --- Redis / Celery ---
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # --- MinIO ---
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "jalnetra"
    minio_secret_key: str = "jalnetra123"
    minio_bucket: str = "jalnetra"
    minio_secure: bool = False

    # --- TiTiler ---
    titiler_url: str = "http://localhost:8001"

    # --- Local data cache (MGRS grid, seed files, cached arrays in non-Docker dev) ---
    data_dir: Path = Path("data")
    s2_grid_kml_url: str = (
        "https://sentinels.copernicus.eu/documents/247904/1955685/"
        "S2A_OPER_GIP_TILPAR_MPC__20151209T095117_V20150622T000000_21000101T000000_B00.kml"
    )

    # --- Satellite ingestion (L3) ---
    stac_source: Literal["earth-search", "cdse"] = "earth-search"
    stac_fallback: bool = True  # try the other source when the primary fails
    earth_search_url: str = "https://earth-search.aws.element84.com/v1"
    cdse_stac_url: str = "https://stac.dataspace.copernicus.eu/v1/"
    cdse_token_url: str = (
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
    )
    cdse_client_id: str | None = None
    cdse_client_secret: str | None = None
    stac_max_cloud_pct: float = 60.0  # tile-level threshold for the scene `usable` flag
    ingest_lookback_days: int = 7  # beat poll window for Tier 1 bodies
    ingest_cache_prefix: str = "cache"  # MinIO key prefix for windowed arrays

    # --- Preprocessing + water mask (L4 + L5) ---
    mask_min_valid_pct: float = 40.0  # reject a scene for a body below this cloud-free share
    chip_prefix: str = "chips"  # MinIO key prefix for COG chips
    mask_after_ingest: bool = True  # ingest task enqueues compute_water_mask for new scenes

    # --- Spectral indicators (L6) ---
    # L2A DN = (reflectance + 0.1) * 10000 for processing baseline >= 04.00 (all scenes
    # since Jan 2022 and the reprocessed archive). Set 0 for legacy pre-2022 products.
    s2_boa_add_offset: int = -1000
    indicator_min_valid_pct: float = 30.0  # reject a zone record below this cloud-free share
    indicator_min_pixels: int = 25  # ... or with fewer aggregated pixels than this
    indicators_after_mask: bool = True  # mask task enqueues compute_indicators when usable

    # --- Seasonal baseline + rainfall covariate (L7) ---
    baseline_window_days: int = 30  # DOY window width, centred on each day-of-year
    baseline_min_samples: int = 5  # below this a window is "building" and alerts are suppressed
    baseline_min_history_days_tier1: int = 730  # Tier 1 needs 2 years of history to be usable
    baseline_min_history_days: int = 365  # Tier 2/3
    baseline_history_years: int = 3  # default span of the bulk historical backfill
    baseline_backfill_chunk_days: int = 31  # one ingest task per chunk of the backfill window
    open_meteo_archive_url: str = "https://archive-api.open-meteo.com/v1/archive"
    open_meteo_forecast_url: str = "https://api.open-meteo.com/v1/forecast"
    open_meteo_timeout_s: float = 30.0
    rainfall_archive_lag_days: int = 5  # ERA5 archive trails real time by ~5 days
    rainfall_lookback_days: int = 10  # daily job re-fetches this many days (covers the lag)
    rainfall_history_start: str = "2017-01-01"  # earliest day the bulk rainfall backfill fetches

    # --- Anomaly detection (L8) ---
    anomaly_z_threshold: float = 3.0  # |robust z| above this flags an indicator
    anomaly_z_high: float = 5.0  # ... and above this a single detector already means "high"
    # Sigma floor per indicator: a zone whose history is nearly constant would
    # otherwise turn measurement noise into a huge z. Roughly 1 x sensor noise.
    anomaly_sigma_floor: dict[str, float] = Field(
        default_factory=lambda: {
            "ndti_turbidity": 0.02,
            "ndci_chlorophyll": 0.02,
            "fai_algal": 0.005,
            "sediment_proxy": 0.005,
            "mndwi_extent": 0.05,
        }
    )
    spatial_enabled: bool = True
    spatial_pixel_z: float = 3.0  # per-pixel robust z (within-scene) above which a pixel is "hot"
    spatial_eps_px: float = 3.0  # DBSCAN neighbourhood radius in pixels
    spatial_min_samples: int = 10  # DBSCAN core-point minimum
    spatial_min_area_km2: float = 0.05  # cluster area below this is not a spatial anomaly
    spatial_max_hot_fraction: float = (
        0.30  # more hot water than this is a body-wide shift, not a plume
    )
    spatial_max_hot_pixels: int = 200_000  # DBSCAN guard at very large bodies
    multivariate_min_history: int = 20  # scenes needed before IsolationForest is fitted
    multivariate_contamination: float = 0.05
    multivariate_seed: int = 42
    rainfall_gate_window_days: int = 30  # DOY window for the historical mm_72h p90
    rainfall_gate_min_history: int = (
        30  # days of rainfall history in the window before the gate can fire
    )
    anomalies_after_indicators: bool = True  # indicator task enqueues detect_anomalies

    # --- Fusion, priority, explainability (L9 + L10) ---
    priority_model_prefix: str = "models/priority"  # MinIO prefix for trained boosters
    priority_train_min_validations: int = (
        50  # below this, training refuses and the weighted model stays
    )
    priority_z_saturation: float = 5.0  # a temporal z of this or more counts as a full deviation
    score_after_anomalies: bool = True  # detect_anomalies enqueues score_candidates

    # --- Alerts + delivery (L11 + L12) ---
    alert_dedupe_days: int = (
        14  # an open alert for the same zone+indicator absorbs new observations
    )
    alert_min_priority: float = 0.0  # alertable candidates below this never become alerts
    alerts_after_scoring: bool = True  # score_candidates enqueues assemble_alerts
    brief_prefix: str = "briefs"  # MinIO key prefix for investigation-brief PDFs
    brief_series_months: int = 12  # evidence timeline span in the brief
    public_base_url: str = "http://localhost:8000"  # absolute links in e-mails / webhooks
    dispatch_enabled: bool = False  # master switch: never deliver from a dev box by accident
    dispatch_timeout_s: float = 15.0
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "jalnetra@localhost"
    smtp_starttls: bool = True

    # --- API (L2) ---
    api_cache_ttl_s: int = 300  # Redis TTL for series / indicator responses
    api_cache_enabled: bool = True
    rate_limit_default: str = "240/minute"  # slowapi syntax, per client IP
    rate_limit_tiles: str = "2000/minute"
    tile_cache_max_age_s: int = 3600  # Cache-Control on proxied tiles (chips are immutable)

    # --- Health ---
    health_check_timeout_s: float = 3.0

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @property
    def database_url_sync(self) -> str:
        """Same DSN on the sync psycopg driver, for Alembic and CLI scripts."""
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
