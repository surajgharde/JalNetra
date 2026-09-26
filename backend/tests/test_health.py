import asyncio

import pytest
from httpx import AsyncClient

from app.core import health as health_mod
from app.core.config import Settings
from app.schemas.health import ServiceStatus


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
    # Optional probes are switched on by the environment (GEE_ENABLED); clear
    # them so "every check passes" means these three, wherever this runs.
    monkeypatch.setattr(health_mod, "OPTIONAL_CHECKS", {}, raising=True)


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
    assert r.json()["services"]["postgres"]["error"].startswith("timeout after")


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


# --- probe timeouts --------------------------------------------------------------
#
# stac and gee cross the internet, where a TLS handshake alone can outlast the local
# ceiling. Holding them to it made /health answer 503 degraded -- enough for a load
# balancer to depool the API -- while every dependency was in fact answering.


def test_remote_probes_get_the_longer_ceiling() -> None:
    settings = Settings(app_env="test", health_check_timeout_s=3.0, health_remote_timeout_s=20.0)
    assert health_mod._timeout_for("postgres", settings) == 3.0
    assert health_mod._timeout_for("redis", settings) == 3.0
    assert health_mod._timeout_for("minio", settings) == 3.0
    assert health_mod._timeout_for("stac", settings) == 20.0
    assert health_mod._timeout_for("gee", settings) == 20.0


def test_remote_ceiling_never_undercuts_the_local_one() -> None:
    """Lowering only the remote knob must not make remote probes stricter than local."""
    settings = Settings(app_env="test", health_check_timeout_s=10.0, health_remote_timeout_s=1.0)
    assert health_mod._timeout_for("stac", settings) == 10.0


async def test_a_slow_remote_probe_no_longer_degrades_health(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stac probe slower than the local ceiling but inside the remote one is ok."""

    async def slow_stac(settings: Settings) -> None:
        await asyncio.sleep(0.2)

    monkeypatch.setenv("HEALTH_CHECK_TIMEOUT_S", "0.05")
    monkeypatch.setenv("HEALTH_REMOTE_TIMEOUT_S", "5")
    monkeypatch.setattr(health_mod, "CHECKS", {"stac": slow_stac}, raising=True)
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["services"]["stac"]["status"] == "ok"


# --- what a failure means --------------------------------------------------------
#
# 503 tells a load balancer to depool the instance. A remote satellite source going
# quiet does not warrant that: everything but live imagery and fresh ingestion reads
# the database and keeps working, and there is no healthier instance to fail over to.


async def test_a_down_remote_source_is_reported_but_keeps_the_api_in_rotation(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_gee(settings: Settings) -> None:
        raise RuntimeError("EOF occurred in violation of protocol")

    async def fine(settings: Settings) -> None:
        return None

    monkeypatch.setattr(health_mod, "CHECKS", {"postgres": fine, "gee": broken_gee}, raising=True)
    r = await client.get("/health")
    assert r.status_code == 200, "a remote blip must not depool the API"
    body = r.json()
    assert body["status"] == "degraded"  # still honest: the UI banner names it
    assert body["services"]["gee"]["status"] == "error"
    assert body["services"]["postgres"]["status"] == "ok"


async def test_a_down_core_dependency_still_returns_503(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_pg(settings: Settings) -> None:
        raise RuntimeError("could not connect")

    monkeypatch.setattr(health_mod, "CHECKS", {"postgres": broken_pg}, raising=True)
    r = await client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"


def test_core_is_healthy_ignores_only_the_remote_probes() -> None:
    err = ServiceStatus(status="error", latency_ms=1.0, error="boom")
    ok = ServiceStatus(status="ok", latency_ms=1.0)
    assert health_mod.core_is_healthy({"postgres": ok, "gee": err, "stac": err}) is True
    assert health_mod.core_is_healthy({"postgres": err, "gee": ok}) is False
    assert health_mod.core_is_healthy({"redis": err}) is False
    assert health_mod.core_is_healthy({"minio": err}) is False
    # an unlisted, non-remote probe counts as core by default
    assert health_mod.core_is_healthy({"something_new": err}) is False
