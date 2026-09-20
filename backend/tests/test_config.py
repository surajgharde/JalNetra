import pytest

from app.core.config import Settings


def test_defaults_are_local_dev() -> None:
    s = Settings(_env_file=None)
    assert s.app_env == "dev"
    assert s.database_url.startswith("postgresql+asyncpg://")
    assert s.minio_bucket == "jalnetra"


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("CORS_ORIGINS", '["https://jalnetra.example"]')
    s = Settings(_env_file=None)
    assert s.app_env == "prod"
    assert s.log_level == "DEBUG"
    assert s.cors_origins == ["https://jalnetra.example"]


def test_sync_url_swaps_driver() -> None:
    s = Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@h:5432/db")
    assert s.database_url_sync == "postgresql+psycopg://u:p@h:5432/db"
