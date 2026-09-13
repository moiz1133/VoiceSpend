"""The Celery application — broker only, no result backend (nothing here
reads a task's return value back through Celery; recompute_amount_base's
own DB write is the durable result).

Worker metrics: FX task counters (app/core/metrics.py's
FX_LAST_SUCCESSFUL_INGEST / FX_INGEST_FAILURES) are incremented inside
task code, which for a Celery prefork worker runs in forked *child*
processes — invisible to a metrics scrape unless something in the parent
aggregates them. On worker_ready (fired once in the parent, before any
child is forked), we start a small standalone Prometheus HTTP server
using a MultiProcessCollector over this container's own local
PROMETHEUS_MULTIPROC_DIR — NOT shared with the api container/service,
since prometheus_client's per-process file names are PID-based and PIDs
are only unique within one container's PID namespace. Prometheus scrapes
this worker as its own target (see prometheus/prometheus.yml), separate
from the api's /metrics.
"""

import logging
import os

from celery import Celery
from celery.signals import worker_ready

from app.core.config import get_settings
from app.worker.schedules import build_beat_schedule

logger = logging.getLogger(__name__)

_WORKER_METRICS_PORT = 9808

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
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        _start_worker_metrics_server()


def _start_worker_metrics_server() -> None:
    from prometheus_client import CollectorRegistry, multiprocess, start_http_server

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
    start_http_server(_WORKER_METRICS_PORT, registry=registry)
    logger.info("worker metrics exposed on :%d/metrics", _WORKER_METRICS_PORT)


# Imported for its side effect of registering tasks on `celery_app` —
# must come after `celery_app` is assigned above (tasks.fx imports it back).
import app.worker.tasks.fx  # noqa: E402,F401
