import json
import logging

from app.core.context import bind_job_id, bind_request_id, job_id_var, request_id_var
from app.core.logging import JsonFormatter


def _record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, msg, (), None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_formatter_emits_json_with_ids() -> None:
    t1 = bind_request_id("req-1")
    t2 = bind_job_id("job-1")
    try:
        out = json.loads(JsonFormatter().format(_record("x", water_body_id="wb_1")))
    finally:
        request_id_var.reset(t1)
        job_id_var.reset(t2)

    assert out["msg"] == "x"
    assert out["level"] == "INFO"
    assert out["request_id"] == "req-1"
    assert out["job_id"] == "job-1"
    assert out["water_body_id"] == "wb_1"
    assert out["ts"].endswith("+00:00")


def test_ids_are_null_outside_context() -> None:
    out = json.loads(JsonFormatter().format(_record()))
    assert out["request_id"] is None
    assert out["job_id"] is None


def test_exception_is_serialised() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        rec = _record("failed")
        rec.exc_info = __import__("sys").exc_info()
    out = json.loads(JsonFormatter().format(rec))
    assert "ValueError: boom" in out["exc_info"]
