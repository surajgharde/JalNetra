import logging

from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(name="app.workers.tasks.ping")
def ping() -> str:
    log.info("ping")
    return "pong"


@celery_app.task(name="app.workers.tasks.heartbeat")
def heartbeat() -> None:
    log.info("beat heartbeat")
