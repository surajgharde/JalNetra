"""Request- and job-scoped identifiers, propagated through contextvars so every
log record can carry them without threading arguments through call sites."""

import uuid
from contextvars import ContextVar, Token

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)


def new_request_id() -> str:
    return uuid.uuid4().hex


def bind_request_id(value: str) -> Token[str | None]:
    return request_id_var.set(value)


def bind_job_id(value: str) -> Token[str | None]:
    return job_id_var.set(value)


def get_request_id() -> str | None:
    return request_id_var.get()


def get_job_id() -> str | None:
    return job_id_var.get()
