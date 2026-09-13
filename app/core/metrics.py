"""Metrics emission for FX ingestion (Phase 6).

Prometheus isn't wired into this project until Phase 7, so these emit
structured log lines with a stable `metric` key instead of no-op'ing
silently — greppable/alertable today (last_successful_ingest as a log
timestamp lets you alert on stale rates via log-based monitoring), and a
straightforward swap for real prometheus_client Counter/Gauge objects
later without touching any call site in app/worker/tasks/fx.py.
"""

import logging
from datetime import UTC, datetime

logger = logging.getLogger("app.metrics")


def record_fx_ingest_success(currencies_written: int) -> None:
    logger.info(
        "metric=fx_ingest_success value=1 currencies_written=%d last_successful_ingest=%s",
        currencies_written,
        datetime.now(UTC).isoformat(),
    )


def record_fx_ingest_failure(error: str) -> None:
    logger.error("metric=fx_ingest_failure value=1 error=%s", error)


def record_amount_base_recomputed(count: int) -> None:
    logger.info("metric=amount_base_recomputed_total value=%d", count)
