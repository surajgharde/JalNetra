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
