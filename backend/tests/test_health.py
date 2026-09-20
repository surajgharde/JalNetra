import pytest
from httpx import AsyncClient

from app.core import health as health_mod
from app.core.config import Settings


async def _ok(settings: Settings) -> None:
    return None


async def _fail(settings: Settings) -> None:
    raise ConnectionError("refused")


async def _hang(settings: Settings) -> None:
    import asyncio

    await asyncio.sleep(10)


@pytest.fixture
def all_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        health_mod, "CHECKS", {"postgres": _ok, "redis": _ok, "minio": _ok}, raising=True
    )


async def test_health_ok(client: AsyncClient, all_ok: None) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert set(body["services"]) == {"postgres", "redis", "minio"}
    assert all(s["status"] == "ok" for s in body["services"].values())
    assert body["app"] == "JalNetra"


async def test_health_degraded_returns_503(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        health_mod, "CHECKS", {"postgres": _ok, "redis": _fail, "minio": _ok}, raising=True
    )
    r = await client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["services"]["redis"]["status"] == "error"
    assert "ConnectionError" in body["services"]["redis"]["error"]
    assert body["services"]["postgres"]["status"] == "ok"


async def test_health_timeout_is_reported(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEALTH_CHECK_TIMEOUT_S", "0.05")
    monkeypatch.setattr(health_mod, "CHECKS", {"postgres": _hang}, raising=True)
    r = await client.get("/health")
    assert r.status_code == 503
    assert r.json()["services"]["postgres"]["error"] == "timeout"


async def test_request_id_is_generated(client: AsyncClient, all_ok: None) -> None:
    r = await client.get("/health")
    assert len(r.headers["x-request-id"]) == 32


async def test_request_id_is_echoed(client: AsyncClient, all_ok: None) -> None:
    r = await client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"


@pytest.mark.integration
async def test_health_against_live_stack(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
