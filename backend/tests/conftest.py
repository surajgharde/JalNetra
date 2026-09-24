import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings

INTEGRATION = os.environ.get("JALNETRA_INTEGRATION") == "1"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if INTEGRATION:
        return
    skip = pytest.mark.skip(reason="set JALNETRA_INTEGRATION=1 to run against the live stack")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _close_cached_redis() -> AsyncIterator[None]:
    """The API cache keeps one lru_cached client, but every test gets its own
    event loop. Without this the client outlives its loop and a later test dies
    in `Event loop is closed` while the old connection is torn down."""
    yield
    from app.core.cache import close_redis

    await close_redis()


@pytest.fixture
def settings() -> Settings:
    return Settings(app_env="test")


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
