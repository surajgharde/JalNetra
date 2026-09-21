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
