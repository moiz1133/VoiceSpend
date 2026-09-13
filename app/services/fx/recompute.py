"""Fills expenses.amount_base where it's NULL.

Uses app/services/currency/converter.py — the SAME converter the stateless
parse pipeline uses — as the single source of truth for the conversion
math. This module only owns selection, batching, and writing.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Expense, User
from app.services.currency.converter import convert


@dataclass(frozen=True)
class RecomputeResult:
    scanned: int
    recomputed: int
    still_missing: int


async def recompute_amount_base(
    db: AsyncSession, *, user_id: UUID | None = None, limit: int | None = None
) -> RecomputeResult:
    """Processes up to `limit` NULL-amount_base rows in one bounded batch —
    never the whole backlog in a single transaction (see
    settings.FX_RECOMPUTE_BATCH).

    Writes go through a plain SQL UPDATE (not an ORM attribute assignment
    on a loaded instance) so the expenses.server_seq trigger fires exactly
    like it does for any other write to the table — that's what makes an
    enriched row show up on the next /api/v1/sync pull for every device.
    Only amount_base/currency_base are ever set here; every client-owned
    column is left untouched.
    """
    limit = limit or get_settings().FX_RECOMPUTE_BATCH

    query = (
        select(
            Expense.id,
            Expense.amount_original,
            Expense.currency_original,
            Expense.spent_at,
            User.base_currency,
        )
        .join(User, User.id == Expense.user_id)
        .where(Expense.amount_base.is_(None), Expense.deleted_at.is_(None))
        .order_by(Expense.created_at)
        .limit(limit)
    )
    if user_id is not None:
        query = query.where(Expense.user_id == user_id)

    rows = (await db.execute(query)).all()

    recomputed = 0
    for row in rows:
        amount_base = await convert(
            db, row.amount_original, row.currency_original, row.base_currency, row.spent_at.date()
        )
        if amount_base is None:
            continue
        await db.execute(
            update(Expense)
            .where(Expense.id == row.id)
            .values(amount_base=amount_base, currency_base=row.base_currency)
        )
        recomputed += 1

    await db.commit()
    return RecomputeResult(
        scanned=len(rows), recomputed=recomputed, still_missing=len(rows) - recomputed
    )


async def null_amount_base_for_user(db: AsyncSession, user_id: UUID) -> int:
    """Re-arms every one of a user's expenses for recompute after their
    base_currency changes.

    amount_base is a pure function of (amount_original, currency_original,
    spent_at, base_currency) — the same invalidation logic the sync upsert
    applies per-row when amount_original/currency_original/spent_at edits
    (see app/api/v1/sync.py), just at user scope instead of row scope,
    since a base_currency change invalidates every one of that user's
    rows at once. No base_currency-change endpoint exists yet (out of
    scope for Phase 6); this is the null half of that future wiring — see
    app/worker/tasks/fx.py:handle_base_currency_changed for the enqueue half.
    """
    result = await db.execute(
        update(Expense)
        .where(Expense.user_id == user_id, Expense.amount_base.isnot(None))
        .values(amount_base=None, currency_base=None)
    )
    await db.commit()
    return result.rowcount or 0  # type: ignore[attr-defined]
