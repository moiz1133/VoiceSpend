"""The single Prometheus metrics registry for this app.

LABEL CARDINALITY — the one rule that matters here: every label below
comes from a small, bounded set (route *templates*, HTTP method, status
code, provider/model names, a fixed status enum, a fixed outcome enum).
NEVER add user_id, device_id, trace_id, merchant, or any other per-entity
value as a label — Prometheus keeps a distinct time series per unique
label combination, so an unbounded label silently creates unbounded
series and eventually takes down the TSDB. Enforce this in review.

MULTIPROCESS MODE: when PROMETHEUS_MULTIPROC_DIR is set, prometheus_client
switches every metric to an mmap-backed value class shared across
processes in the SAME container (multiple uvicorn/gunicorn workers, or a
Celery prefork worker's forked children) — but that decision is latched
in the first time `prometheus_client` is imported anywhere in the
process, so the directory must be cleaned/created and prometheus_client
must be imported for the first time right here, before any Counter/
Histogram/Gauge is constructed. Every other module in this app that needs
a metric imports it FROM HERE rather than importing prometheus_client
directly, so this file is guaranteed to be the first (and only) importer.
"""

import os
import shutil

_MULTIPROC_DIR = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
if _MULTIPROC_DIR:
    shutil.rmtree(_MULTIPROC_DIR, ignore_errors=True)
    os.makedirs(_MULTIPROC_DIR, exist_ok=True)

import logging  # noqa: E402
from datetime import UTC, datetime  # noqa: E402

from prometheus_client import (  # noqa: E402
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

logger = logging.getLogger("app.metrics")

PROMETHEUS_CONTENT_TYPE = CONTENT_TYPE_LATEST

_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10)

# --- HTTP -------------------------------------------------------------
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["route", "method", "status"],
    buckets=_LATENCY_BUCKETS,
)

# --- Parse pipeline -----------------------------------------------------
PARSE_REQUESTS = Counter(
    "parse_requests_total",
    "Parse pipeline requests",
    ["provider", "model", "status"],
)
PARSE_LATENCY = Histogram(
    "parse_latency_seconds",
    "Parse pipeline stage latency",
    ["provider", "stage"],  # stage in {stt, llm, total}
    buckets=_LATENCY_BUCKETS,
)
PARSE_LLM_COST_USD = Counter(
    "parse_llm_cost_usd_total",
    "Cumulative best-effort LLM cost in USD — the unit-economics gauge",
    ["provider", "model"],
)
PARSE_TOKENS = Counter(
    "parse_tokens_total",
    "LLM tokens consumed",
    ["provider", "model", "kind"],  # kind in {prompt, completion}
)

# --- Rate limiting --------------------------------------------------------
RATE_LIMITED = Counter(
    "rate_limited_total", "Requests rejected by the per-device rate limiter", ["endpoint"]
)
RATE_LIMITER_REDIS_ERRORS = Counter(
    "rate_limiter_redis_error_total", "Redis errors encountered by the rate limiter"
)

# --- Sync -----------------------------------------------------------------
SYNC_PUSH_RECORDS = Counter(
    "sync_push_records_total",
    "Sync push records by result",
    ["result"],  # applied | stale_ignored | rejected
)
SYNC_PUSH_NEW = Counter(
    "sync_push_new_total", "Genuinely new expenses created via sync push (was_inserted=true)"
)

# --- FX ingestion (Phase 6, wired into the real registry now) -------------
FX_LAST_SUCCESSFUL_INGEST = Gauge(
    "fx_last_successful_ingest_timestamp",
    "Unix timestamp of the last successful FX ingest — alert if this goes stale (> 26h)",
    multiprocess_mode="max",
)
FX_INGEST_FAILURES = Counter("fx_ingest_failure_total", "FX ingest failures")

# --- Billing webhook --------------------------------------------------
WEBHOOK_EVENTS = Counter(
    "webhook_events_total",
    "Billing webhook events processed",
    ["event_type", "outcome"],  # outcome in {applied, duplicate, stale, unmatched}
)


def render_prometheus_metrics() -> bytes:
    """Renders the current metrics in Prometheus text exposition format.
    Under multiprocess mode this aggregates every sibling process's (or
    forked child's) local mmap files in _MULTIPROC_DIR; otherwise it just
    reads this process's own in-memory registry.
    """
    if _MULTIPROC_DIR:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
    else:
        registry = REGISTRY
    return generate_latest(registry)


def record_rate_limited(endpoint: str) -> None:
    RATE_LIMITED.labels(endpoint=endpoint).inc()


def record_rate_limiter_redis_error() -> None:
    RATE_LIMITER_REDIS_ERRORS.inc()


def record_parse_request(*, provider: str, model: str, status: str) -> None:
    PARSE_REQUESTS.labels(provider=provider, model=model, status=status).inc()


def record_parse_latency(*, provider: str, stage: str, seconds: float) -> None:
    PARSE_LATENCY.labels(provider=provider, stage=stage).observe(seconds)


def record_parse_cost(*, provider: str, model: str, cost_usd: float) -> None:
    if cost_usd > 0:
        PARSE_LLM_COST_USD.labels(provider=provider, model=model).inc(cost_usd)


def record_parse_tokens(*, provider: str, model: str, kind: str, count: int | None) -> None:
    if count:
        PARSE_TOKENS.labels(provider=provider, model=model, kind=kind).inc(count)


def record_sync_push_result(result: str, count: int = 1) -> None:
    if count:
        SYNC_PUSH_RECORDS.labels(result=result).inc(count)


def record_sync_push_new(count: int) -> None:
    if count:
        SYNC_PUSH_NEW.inc(count)


def record_webhook_event(*, event_type: str, outcome: str) -> None:
    WEBHOOK_EVENTS.labels(event_type=event_type, outcome=outcome).inc()


def record_fx_ingest_success(currencies_written: int) -> None:
    FX_LAST_SUCCESSFUL_INGEST.set_to_current_time()
    logger.info(
        "fx ingest succeeded",
        extra={
            "metric": "fx_ingest_success",
            "currencies_written": currencies_written,
            "last_successful_ingest": datetime.now(UTC).isoformat(),
        },
    )


def record_fx_ingest_failure(error: str) -> None:
    FX_INGEST_FAILURES.inc()
    logger.error("fx ingest failed", extra={"metric": "fx_ingest_failure", "error": error})


def record_amount_base_recomputed(count: int) -> None:
    logger.info(
        "amount_base recompute sweep completed",
        extra={"metric": "amount_base_recomputed_total", "count": count},
    )
