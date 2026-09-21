"""S12 tests: queue split, beat schedule, metrics registry, STAC fallback
accounting, CLI parser, and the compose / Prometheus files parsing."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from app.cli import build_parser
from app.core import metrics
from app.main import app
from app.services.l03_ingestion.stac import ChainedSource, SourceError
from app.workers.celery_app import celery_app

ROOT = Path(__file__).resolve().parents[2]


def test_queues_split_by_cost_and_every_task_routed() -> None:
    routes = celery_app.conf.task_routes
    queues = {r["queue"] for r in routes.values()}
    assert queues == {"ingestion", "processing", "scoring", "reporting"}
    assert routes["app.workers.tasks.ingest_water_body"]["queue"] == "ingestion"
    assert routes["app.workers.tasks.compute_water_mask"]["queue"] == "processing"
    assert routes["app.workers.tasks.score_candidates"]["queue"] == "scoring"
    assert routes["app.workers.tasks.generate_brief"]["queue"] == "reporting"
    registered = {n for n in celery_app.tasks if n.startswith("app.workers.tasks.")}
    unrouted = registered - set(routes) - {"app.workers.tasks.ping", "app.workers.tasks.heartbeat"}
    assert not unrouted, unrouted


def test_beat_schedule_matches_plan() -> None:
    beat = celery_app.conf.beat_schedule
    tiers = {beat[k]["args"][0]: beat[k]["schedule"] for k in beat if k.startswith("poll-tier")}
    assert set(tiers) == {1, 2, 3}
    assert tiers[1].hour == {0, 6, 12, 18}  # every 6 h
    assert len(tiers[2].hour) == 1 and len(tiers[2].day_of_week) == 7  # daily
    assert len(tiers[3].day_of_week) == 1  # weekly
    rain = beat["sync-rainfall-daily"]["schedule"]
    assert (next(iter(rain.hour)), next(iter(rain.minute))) == (20, 30)  # 02:00 IST
    monthly = beat["rebuild-baselines-monthly"]["schedule"]
    assert len(monthly.day_of_month) == 1
    assert "refresh-ops-gauges" in beat and "retrain-priority-model-weekly" in beat


def test_metrics_registry_and_endpoint() -> None:
    metrics.scenes_ingested.labels(source="earth-search").inc()
    metrics.alerts_gated_rainfall.inc()
    metrics.task_duration.labels(stage="mask", status="success").observe(1.5)
    text = metrics.render().decode()
    for name in (
        "jalnetra_scenes_ingested_total",
        "jalnetra_scenes_rejected_cloud_total",
        "jalnetra_zones_processed_total",
        "jalnetra_alerts_raised_total",
        "jalnetra_alerts_gated_rainfall_total",
        "jalnetra_task_duration_seconds",
        "jalnetra_stac_request_failures_total",
        "jalnetra_tier1_days_since_usable_scene",
    ):
        assert name in text, name
    assert metrics.stage_of("app.workers.tasks.compute_water_mask") == "mask"
    with TestClient(app) as client:
        r = client.get("/metrics")
        assert r.status_code == 200 and "jalnetra_task_duration_seconds" in r.text


class _Src:
    def __init__(self, name: str, fail: bool) -> None:
        self.name, self.fail = name, fail

    def search(self, *_: Any, **__: Any) -> list[Any]:
        if self.fail:
            raise SourceError(f"{self.name} down")
        return [f"scene-from-{self.name}"]

    def gdal_env(self) -> dict[str, str]:
        return {}


def _counter(c: Any, **labels: str) -> float:
    return float(c.labels(**labels)._value.get())


def test_stac_fallback_serves_scenes_and_is_counted() -> None:
    """Plan acceptance: the primary dying causes automatic fallback with no lost scenes."""
    before_fail = _counter(metrics.stac_request_failures, source="earth-search")
    before_fb = _counter(metrics.stac_fallbacks, served_by="cdse")
    chain = ChainedSource([_Src("earth-search", fail=True), _Src("cdse", fail=False)])  # type: ignore[list-item]
    found: Any = chain.search(None, date(2026, 9, 1), date(2026, 9, 2))
    assert found == ["scene-from-cdse"]
    assert _counter(metrics.stac_request_failures, source="earth-search") == before_fail + 1
    assert _counter(metrics.stac_fallbacks, served_by="cdse") == before_fb + 1
    healthy = ChainedSource([_Src("earth-search", fail=False), _Src("cdse", fail=False)])  # type: ignore[list-item]
    served: Any = healthy.search(None, date(2026, 9, 1), date(2026, 9, 2))
    assert served == ["scene-from-earth-search"]
    assert _counter(metrics.stac_fallbacks, served_by="cdse") == before_fb + 1  # unchanged


def test_cli_parser() -> None:
    p = build_parser()
    a = p.parse_args(
        [
            "backfill",
            "--water-body",
            "wb_x",
            "--from",
            "2023-01-01",
            "--to",
            "2026-09-01",
            "--resume",
        ]
    )
    assert a.command == "backfill" and a.resume and a.date_from == "2023-01-01"
    assert p.parse_args(["backfill-status"]).command == "backfill-status"
    assert p.parse_args(["ops-gauges"]).command == "ops-gauges"


def test_compose_and_prometheus_files_parse() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    pools = {k for k in services if k.startswith("worker-")}
    assert pools == {"worker-ingestion", "worker-processing", "worker-scoring", "worker-reporting"}
    for name in pools:
        q = name.removeprefix("worker-")
        assert (
            f"-Q {q}" in services[name]["command"] or f"-Q default,{q}" in services[name]["command"]
        )
    assert (
        services["flower"]["profiles"] == ["ops"]
        and "prometheus" in services
        and "grafana" in services
    )
    prom = yaml.safe_load((ROOT / "infra/prometheus/prometheus.yml").read_text(encoding="utf-8"))
    targets = {
        t for sc in prom["scrape_configs"] for st in sc["static_configs"] for t in st["targets"]
    }
    assert {"api:8000", "worker-ingestion:9100", "worker-reporting:9100"} <= targets
    rules = yaml.safe_load((ROOT / "infra/prometheus/alerts.yml").read_text(encoding="utf-8"))
    names = {r["alert"] for g in rules["groups"] for r in g["rules"]}
    assert "Tier1WaterBodyStale" in names
    stale = next(
        r for g in rules["groups"] for r in g["rules"] if r["alert"] == "Tier1WaterBodyStale"
    )
    assert "> 10" in stale["expr"] and stale["labels"]["severity"] == "page"
