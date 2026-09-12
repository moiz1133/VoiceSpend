"""Tests for the deterministic USD-pivot FX converter.

fx_rates is empty until Phase 6's backfill job — every "missing rate"
case here must resolve to None, never an exception.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FxRate
from app.services.currency.converter import convert


async def test_same_currency_short_circuits(db_session: AsyncSession) -> None:
    result = await convert(db_session, Decimal("10.00"), "USD", "USD", date(2026, 1, 1))
    assert result == Decimal("10.00")


async def test_missing_rate_returns_none_not_error(db_session: AsyncSession) -> None:
    assert await convert(db_session, Decimal("10"), "USD", "XYZ", date(2026, 1, 1)) is None
    assert await convert(db_session, Decimal("10"), "XYZ", "USD", date(2026, 1, 1)) is None
    assert await convert(db_session, Decimal("10"), "ABC", "XYZ", date(2026, 1, 1)) is None


async def test_usd_to_other_currency(db_session: AsyncSession) -> None:
    db_session.add(
        FxRate(currency_code="EUR", rate_date=date(2026, 1, 1), rate_per_usd=Decimal("0.90000000"))
    )
    await db_session.commit()

    result = await convert(db_session, Decimal("100"), "USD", "EUR", date(2026, 1, 1))
    assert result == Decimal("90.0000")


async def test_other_currency_to_usd(db_session: AsyncSession) -> None:
    db_session.add(
        FxRate(currency_code="EUR", rate_date=date(2026, 1, 1), rate_per_usd=Decimal("0.90000000"))
    )
    await db_session.commit()

    result = await convert(db_session, Decimal("90"), "EUR", "USD", date(2026, 1, 1))
    assert result == Decimal("100.0000")


async def test_cross_currency_via_usd_pivot(db_session: AsyncSession) -> None:
    db_session.add_all(
        [
            FxRate(
                currency_code="EUR", rate_date=date(2026, 1, 1), rate_per_usd=Decimal("0.90000000")
            ),
            FxRate(
                currency_code="PKR",
                rate_date=date(2026, 1, 1),
                rate_per_usd=Decimal("280.00000000"),
            ),
        ]
    )
    await db_session.commit()

    result = await convert(db_session, Decimal("100"), "EUR", "PKR", date(2026, 1, 1))
    expected = (Decimal("100") * (Decimal("280.00000000") / Decimal("0.90000000"))).quantize(
        Decimal("0.0001")
    )
    assert result == expected


async def test_uses_latest_rate_on_or_before_as_of_date(db_session: AsyncSession) -> None:
    db_session.add_all(
        [
            FxRate(
                currency_code="EUR", rate_date=date(2026, 1, 1), rate_per_usd=Decimal("0.90000000")
            ),
            FxRate(
                currency_code="EUR", rate_date=date(2026, 1, 10), rate_per_usd=Decimal("0.95000000")
            ),
            # A future rate must never be used for an earlier as_of date.
            FxRate(
                currency_code="EUR", rate_date=date(2026, 2, 1), rate_per_usd=Decimal("1.50000000")
            ),
        ]
    )
    await db_session.commit()

    result = await convert(db_session, Decimal("100"), "USD", "EUR", date(2026, 1, 15))
    assert result == Decimal("95.0000")  # picks Jan 10, the latest rate <= Jan 15
