"""Deterministic FX conversion — USD-pivot, best-effort, display-only.

This is NOT the authoritative FX record — Phase 6's backfill job owns
writing expenses.amount_base for real. This converter only produces a
best-effort value for the parse response so the client has something
sensible to show immediately; it is never persisted by this phase.

fx_rates is empty until Phase 6 seeds it, so a missing rate is an expected,
non-error outcome here — every caller treats None as "not known yet", not
a failure.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FxRate

_USD = "USD"
_QUANTIZE = Decimal("0.0001")


async def _rate_per_usd(db: AsyncSession, currency_code: str, as_of: date) -> Decimal | None:
    if currency_code == _USD:
        return Decimal(1)
    result = await db.execute(
        select(FxRate.rate_per_usd)
        .where(FxRate.currency_code == currency_code, FxRate.rate_date <= as_of)
        .order_by(FxRate.rate_date.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def convert(
    db: AsyncSession,
    amount: Decimal,
    from_currency: str,
    to_currency: str,
    as_of: date,
) -> Decimal | None:
    """amount_base = amount * (rate_to_per_usd / rate_from_per_usd), using
    each currency's rate for `as_of` (or the latest known rate on or before
    it). Returns None — never raises — when a required rate is missing.
    """
    if from_currency == to_currency:
        return amount

    rate_from = await _rate_per_usd(db, from_currency, as_of)
    if rate_from is None:
        return None

    rate_to = await _rate_per_usd(db, to_currency, as_of)
    if rate_to is None:
        return None

    return (amount * (rate_to / rate_from)).quantize(_QUANTIZE)
