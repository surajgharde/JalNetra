"""Celery signal handlers: JSON logging for workers and a job_id bound to every task."""

from contextvars import Token
from typing import Any

from celery.signals import setup_logging, task_postrun, task_prerun

from app.core.config import get_settings
from app.core.context import bind_job_id, bind_request_id, job_id_var, request_id_var
from app.core.logging import configure_logging

_tokens: dict[str, tuple[Token[str | None], Token[str | None] | None]] = {}


@setup_logging.connect
def _configure_worker_logging(**_: Any) -> None:
    configure_logging(get_settings().log_level)


@task_prerun.connect
def _bind_ids(task_id: str, task: Any, **_: Any) -> None:
    job_token = bind_job_id(task_id)
    request_id = (getattr(task.request, "headers", None) or {}).get("request_id")
    req_token = bind_request_id(request_id) if request_id else None
    _tokens[task_id] = (job_token, req_token)


@task_postrun.connect
def _unbind_ids(task_id: str, **_: Any) -> None:
    job_token, req_token = _tokens.pop(task_id, (None, None))
    if job_token is not None:
        job_id_var.reset(job_token)
    if req_token is not None:
        request_id_var.reset(req_token)
