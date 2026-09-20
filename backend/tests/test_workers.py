import io
import json
import logging

import pytest

from app.core.context import get_job_id
from app.workers.celery_app import celery_app
from app.workers.tasks import ping


@pytest.fixture(autouse=True)
def _eager() -> None:
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_store_eager_result = False


def test_ping_runs_eagerly() -> None:
    assert ping.apply().get() == "pong"


def test_job_id_bound_during_task() -> None:
    seen: list[str | None] = []

    @celery_app.task(name="tests.capture_job_id")
    def capture() -> None:
        seen.append(get_job_id())

    result = capture.apply()
    assert seen == [result.id]
    assert get_job_id() is None, "job id must be reset after the task"


def test_beat_schedule_registered() -> None:
    assert "heartbeat-every-5-min" in celery_app.conf.beat_schedule


def test_worker_log_records_carry_job_id() -> None:
    from app.core.logging import JsonFormatter

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("app.workers.tasks")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        result = ping.apply()
    finally:
        logger.removeHandler(handler)

    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    ping_line = next(line for line in lines if line["msg"] == "ping")
    assert ping_line["job_id"] == result.id
