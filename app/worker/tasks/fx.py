"""Celery tasks for FX ingestion and expenses.amount_base enrichment.

These are thin sync entry points — all the real logic lives in
app/services/fx/ (normalization/upsert) and app/services/fx/recompute.py
(the NULL-filling sweep); a task's job is just to own an event loop and a
DB session for the duration of one run (see app/worker/db.py) and call
into those async service functions.
"""

import asyncio
import logging
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import (
    record_amount_base_recomputed,
    record_fx_ingest_failure,
    record_fx_ingest_success,
)
from app.services.fx.factory import get_fx_provider
from app.services.fx.ingest import IngestResult, backfill_range, ingest_snapshot
from app.services.fx.recompute import RecomputeResult
from app.services.fx.recompute import null_amount_base_for_user as _null_amount_base_for_user
from app.services.fx.recompute import recompute_amount_base as _recompute_amount_base
from app.worker.celery_app import celery_app
from app.worker.db import worker_db_session

logger = logging.getLogger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="app.worker.tasks.fx.ingest_daily_fx",
    max_retries=3,
    default_retry_delay=300,
)
def ingest_daily_fx(self) -> dict[str, object]:  # type: ignore[no-untyped-def]
    """Beat-scheduled (~06:00 UTC, see app/worker/schedules.py). On success,
    chains the amount_base recompute sweep so newly-available rates get
    used the same run. On provider failure: log, emit a failure metric,
    and retry with backoff (a few attempts) rather than crashing beat —
    the next scheduled run (or retry) tries again.
    """
    try:
        result = asyncio.run(_ingest_daily_async())
    except Exception as exc:  # noqa: BLE001 - any provider/DB failure -> retry, not a crash
        logger.exception("fx daily ingest failed")
        record_fx_ingest_failure(str(exc))
        raise self.retry(exc=exc) from exc

    record_fx_ingest_success(result.currencies_written)
    recompute_amount_base.delay()
    return {
        "rate_date": result.rate_date.isoformat(),
        "currencies_written": result.currencies_written,
        "skipped": list(result.skipped),
    }


async def _ingest_daily_async() -> IngestResult:
    provider = get_fx_provider()
    rate_date = datetime.now(UTC).date()
    async with worker_db_session() as db:
        values = await provider.fetch_latest()
        return await ingest_snapshot(db, provider=provider, values=values, rate_date=rate_date)


@celery_app.task(name="app.worker.tasks.fx.backfill_fx_range")  # type: ignore[untyped-decorator]
def backfill_fx_range(start_iso: str, end_iso: str) -> list[dict[str, object]]:
    """Manual seed/backfill task, invoked via e.g.:

        uv run celery -A app.worker.celery_app:celery_app call \\
            app.worker.tasks.fx.backfill_fx_range \\
            --args '["2026-09-01", "2026-09-13"]'

    Idempotent by construction — each date's upsert is the same
    ON CONFLICT DO UPDATE the daily job uses, so re-running over an
    overlapping range just refreshes those days' rows.
    """
    start = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    results = asyncio.run(_backfill_async(start, end))
    return [
        {"rate_date": r.rate_date.isoformat(), "currencies_written": r.currencies_written}
        for r in results
    ]


async def _backfill_async(start: date, end: date) -> list[IngestResult]:
    provider = get_fx_provider()
    async with worker_db_session() as db:
        return await backfill_range(db, provider=provider, start=start, end=end)


@celery_app.task(name="app.worker.tasks.fx.recompute_amount_base")  # type: ignore[untyped-decorator]
def recompute_amount_base(
    user_id: str | None = None, limit: int | None = None
) -> dict[str, int]:
    result = asyncio.run(_recompute_batch(UUID(user_id) if user_id else None, limit))
    record_amount_base_recomputed(result.recomputed)
    return {
        "scanned": result.scanned,
        "recomputed": result.recomputed,
        "still_missing": result.still_missing,
    }


async def _recompute_batch(user_id: UUID | None, limit: int | None) -> RecomputeResult:
    async with worker_db_session() as db:
        return await _recompute_amount_base(db, user_id=user_id, limit=limit)


def enqueue_recompute_for_user(user_id: UUID) -> None:
    recompute_amount_base.delay(user_id=str(user_id))


async def handle_base_currency_changed(db: AsyncSession, user_id: UUID) -> int:
    """Wiring for a future base_currency-change endpoint (out of scope for
    Phase 6): null every amount_base for the user, then enqueue a
    recompute scoped to just that user's rows.
    """
    count = await _null_amount_base_for_user(db, user_id)
    enqueue_recompute_for_user(user_id)
    return count
