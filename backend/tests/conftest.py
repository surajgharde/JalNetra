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


@pytest.fixture(autouse=True)
async def _dispose_cached_engine(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """Same problem as the Redis client, for the async database engine.

    ``get_engine`` is lru_cached, so its asyncpg pool is reused across tests
    while each test runs on a fresh event loop. The second test to reach the
    database through the API then finds pooled connections bound to a closed
    loop and fails in `'NoneType' object has no attribute 'send'`.

    Only integration tests get a fresh pool. Disposing after every test instead
    builds and tears down a pool ~230 times in one run, and the suite then dies
    of MemoryError in the matplotlib brief tests long before it finishes."""
    yield
    if "integration" not in request.keywords:
        return
    from app.db.session import dispose_engine

    await dispose_engine()


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
