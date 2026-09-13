"""Monthly usage counters — the free-tier quota metering primitive.

CRITICAL: the period is always derived from the *server's* current time,
never from a client-supplied `spent_at`. A backdatable client field would
let a free user evade quota by claiming every log happened last month.
Since a given expense id is inserted exactly once (Phase 3's idempotent
upsert), it is counted exactly once too — in whichever period it first
actually lands on the server. 30 offline logs from last month that finally
sync today all count against *today's* period; that's intended, not a bug.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UsageCounter


def current_period(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


async def get_usage_count(db: AsyncSession, user_id: uuid.UUID, period: str) -> int:
    result = await db.execute(
        select(UsageCounter.log_count).where(
            UsageCounter.user_id == user_id, UsageCounter.period == period
        )
    )
    return result.scalar_one_or_none() or 0


async def increment_usage(db: AsyncSession, user_id: uuid.UUID, period: str, n: int) -> None:
    """Atomic INSERT ... ON CONFLICT DO UPDATE SET log_count = log_count + n.

    Never read-then-write in Python — concurrent syncs from multiple
    devices for the same user would race and lose an update. No-ops for
    n <= 0 (a replayed batch that inserted nothing new must not touch the
    counter, and must not even take the write lock for it).
    """
    if n <= 0:
        return

    stmt = pg_insert(UsageCounter).values(
        id=uuid.uuid4(), user_id=user_id, period=period, log_count=n
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[UsageCounter.user_id, UsageCounter.period],
        set_={
            "log_count": UsageCounter.log_count + stmt.excluded.log_count,
            "updated_at": func.now(),
        },
    )
    await db.execute(stmt)
