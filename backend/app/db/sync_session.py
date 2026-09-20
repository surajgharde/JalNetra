"""Synchronous engine/session for CLIs and Celery tasks (GeoPandas and rasterio
are blocking libraries; the async engine is for request handlers only)."""

from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


@lru_cache
def get_sync_engine() -> Engine:
    return create_engine(get_settings().database_url_sync, pool_pre_ping=True)


@lru_cache
def get_sync_session_factory() -> sessionmaker[Session]:
    return sessionmaker(get_sync_engine(), expire_on_commit=False)


def sync_session() -> Session:
    return get_sync_session_factory()()
