"""The Celery application — broker only, no result backend (nothing here
reads a task's return value back through Celery; recompute_amount_base's
own DB write is the durable result).
"""

import logging

from celery import Celery
from celery.signals import worker_ready

from app.core.config import get_settings
from app.worker.schedules import build_beat_schedule

logger = logging.getLogger(__name__)

settings = get_settings()

celery_app = Celery("voicespend", broker=settings.celery_broker_url)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    beat_schedule=build_beat_schedule(),
)


@worker_ready.connect  # type: ignore[untyped-decorator]
def _on_worker_ready(**_kwargs: object) -> None:
    logger.info("voicespend celery worker ready")


# Imported for its side effect of registering tasks on `celery_app` —
# must come after `celery_app` is assigned above (tasks.fx imports it back).
import app.worker.tasks.fx  # noqa: E402,F401
