"""Celery signal handlers: JSON logging, a job_id bound to every task, task
duration metrics by stage, Sentry tags (water body + scene), and a Prometheus
endpoint per worker pool (S12)."""

from __future__ import annotations

import logging
import time
from contextvars import Token
from typing import Any

from celery.signals import setup_logging, task_postrun, task_prerun, worker_ready

from app.core import metrics
from app.core.config import get_settings
from app.core.context import bind_job_id, bind_request_id, job_id_var, request_id_var
from app.core.logging import configure_logging
from app.core.observability import clear_tags, init_sentry, set_tags

log = logging.getLogger(__name__)

_tokens: dict[str, tuple[Token[str | None], Token[str | None] | None]] = {}
_started: dict[str, float] = {}
TAG_KEYS = ("water_body_id", "scene_id", "alert_id")


def _task_tags(task: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Pull water_body_id / scene_id / alert_id out of the call, by name or position."""
    tags: dict[str, Any] = {k: kwargs.get(k) for k in TAG_KEYS if k in kwargs}
    names = (
        list(getattr(task, "__wrapped__", task).__code__.co_varnames[:8])
        if hasattr(getattr(task, "__wrapped__", task), "__code__")
        else []
    )
    if names and names[0] == "self":
        names = names[1:]
    for name, value in zip(names, args, strict=False):
        if name in TAG_KEYS and name not in tags:
            tags[name] = value
    return tags


@setup_logging.connect
def _configure_worker_logging(**_: Any) -> None:
    configure_logging(get_settings().log_level)


@worker_ready.connect
def _start_metrics_and_sentry(sender: Any = None, **_: Any) -> None:
    settings = get_settings()
    try:
        queues = ",".join(sorted(q.name for q in sender.consumer.task_consumer.queues))
    except AttributeError:
        queues = ""
    init_sentry(f"worker:{queues or 'default'}", settings=settings)
    if settings.worker_metrics_port:
        from prometheus_client import start_http_server

        try:
            start_http_server(settings.worker_metrics_port, registry=metrics.registry)
            log.info("worker metrics listening", extra={"port": settings.worker_metrics_port})
        except OSError as exc:  # port busy on a shared host: log, do not crash the worker
            log.warning("worker metrics port unavailable", extra={"error": str(exc)})


@task_prerun.connect
def _bind_ids(task_id: str, task: Any, args: Any = (), kwargs: Any = None, **_: Any) -> None:
    job_token = bind_job_id(task_id)
    request_id = (getattr(task.request, "headers", None) or {}).get("request_id")
    req_token = bind_request_id(request_id) if request_id else None
    _tokens[task_id] = (job_token, req_token)
    _started[task_id] = time.perf_counter()
    set_tags({"task": task.name, **_task_tags(task, tuple(args or ()), dict(kwargs or {}))})


@task_postrun.connect
def _unbind_ids(task_id: str, task: Any = None, state: str | None = None, **_: Any) -> None:
    started = _started.pop(task_id, None)
    if started is not None and task is not None:
        metrics.task_duration.labels(
            stage=metrics.stage_of(task.name), status=(state or "UNKNOWN").lower()
        ).observe(time.perf_counter() - started)
    clear_tags(["task", *TAG_KEYS])
    job_token, req_token = _tokens.pop(task_id, (None, None))
    if job_token is not None:
        job_id_var.reset(job_token)
    if req_token is not None:
        request_id_var.reset(req_token)
