"""Tests for FX normalization (USD-pivot) and the upsert orchestration.

normalize_to_usd_pivot is a pure function — no DB needed. The upsert/
snapshot tests need real Postgres (db_session fixture) since they exercise
the actual ON CONFLICT DO UPDATE.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FxRate
from app.services.currency.converter import convert
from app.services.fx.base import FXProvider
from app.services.fx.ingest import backfill_range, ingest_snapshot, normalize_to_usd_pivot

_SCALE = Decimal("0.00000001")


class _StaticProvider(FXProvider):
    provider_name = "fake-fx"

    def __init__(self, base_currency: str, by_date: dict[date, dict[str, Decimal]]) -> None:
        self._base = base_currency
        self._by_date = by_date

    @property
    def base_currency(self) -> str:
        return self._base

    async def fetch_latest(self) -> dict[str, Decimal]:
        return next(iter(self._by_date.values()))

    async def fetch_for_date(self, d: date) -> dict[str, Decimal]:
        return self._by_date.get(d, {})


def test_normalize_eur_base_crosses_via_usd() -> None:
    values = {"USD": Decimal("1.08"), "EUR": Decimal("1.0"), "PKR": Decimal("300.5")}
    normalized = normalize_to_usd_pivot("EUR", values)

    assert normalized["USD"] == Decimal("1.00000000")
    assert normalized["EUR"] == (Decimal("1.0") / Decimal("1.08")).quantize(_SCALE)
    assert normalized["PKR"] == (Decimal("300.5") / Decimal("1.08")).quantize(_SCALE)


def test_normalize_usd_base_passes_through() -> None:
    values = {"EUR": Decimal("0.92"), "PKR": Decimal("278.3")}
    normalized = normalize_to_usd_pivot("USD", values)

    assert normalized["EUR"] == Decimal("0.92").quantize(_SCALE)
    assert normalized["PKR"] == Decimal("278.3").quantize(_SCALE)
    assert normalized["USD"] == Decimal("1.00000000")


def test_normalize_skips_bad_values_but_keeps_good_ones() -> None:
    values = {
        "USD": Decimal("1.0"),
        "ZERO": Decimal("0"),
        "NEG": Decimal("-5"),
        "NAN": Decimal("NaN"),
        "INF": Decimal("Infinity"),
        "GOOD": Decimal("2.5"),
    }
    normalized = normalize_to_usd_pivot("USD", values)

    assert "ZERO" not in normalized
    assert "NEG" not in normalized
    assert "NAN" not in normalized
    assert "INF" not in normalized
    assert normalized["GOOD"] == Decimal("2.50000000")
    assert normalized["USD"] == Decimal("1.00000000")


def test_normalize_missing_usd_leg_only_writes_usd_self_row() -> None:
    values = {"EUR": Decimal("1.0"), "PKR": Decimal("300.5")}  # no USD key at all
    normalized = normalize_to_usd_pivot("EUR", values)

    assert normalized == {"USD": Decimal("1.00000000")}


async def test_idempotent_upsert_refreshes_same_date_row(db_session: AsyncSession) -> None:
    d = date(2026, 3, 1)
    provider = _StaticProvider("USD", {})

    await ingest_snapshot(
        db_session, provider=provider, values={"EUR": Decimal("0.90")}, rate_date=d
    )
    await ingest_snapshot(
        db_session, provider=provider, values={"EUR": Decimal("0.95")}, rate_date=d
    )

    rows = (
        await db_session.execute(
            select(FxRate).where(FxRate.rate_date == d, FxRate.currency_code == "EUR")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].rate_per_usd == Decimal("0.95000000")

    usd_rows = (
        await db_session.execute(
            select(FxRate).where(FxRate.rate_date == d, FxRate.currency_code == "USD")
        )
    ).scalars().all()
    assert len(usd_rows) == 1
    assert usd_rows[0].rate_per_usd == Decimal("1.00000000")


async def test_backfill_range_writes_one_row_per_supported_date(db_session: AsyncSession) -> None:
    by_date = {
        date(2026, 4, 1): {"EUR": Decimal("0.90")},
        date(2026, 4, 2): {"EUR": Decimal("0.91")},
        # 2026-04-03 deliberately absent — stands in for a weekend/holiday gap.
        date(2026, 4, 4): {"EUR": Decimal("0.93")},
    }
    provider = _StaticProvider("USD", by_date)

    results = await backfill_range(
        db_session, provider=provider, start=date(2026, 4, 1), end=date(2026, 4, 4)
    )

    assert [r.rate_date for r in results] == [
        date(2026, 4, 1),
        date(2026, 4, 2),
        date(2026, 4, 3),
        date(2026, 4, 4),
    ]
    # The gap day still ran (provider returned {}) — it writes only the
    # forced USD self-row, never a synthesized EUR value for a day the
    # provider had no data for.
    assert results[2].currencies_written == 1
    assert results[2].skipped == ()

    eur_rows = (
        await db_session.execute(
            select(FxRate.rate_date, FxRate.rate_per_usd)
            .where(FxRate.currency_code == "EUR", FxRate.rate_date.in_(by_date.keys()))
            .order_by(FxRate.rate_date)
        )
    ).all()
    assert [r.rate_date for r in eur_rows] == [date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 4)]


async def test_weekend_gap_converter_falls_back_to_last_known_rate(
    db_session: AsyncSession,
) -> None:
    """2026-04-03 is a Friday, 2026-04-04 a Saturday, 2026-04-05 a Sunday —
    seed only Friday's rate and confirm a Sunday expense converts using it.
    """
    friday = date(2026, 4, 3)
    sunday = date(2026, 4, 5)
    db_session.add(
        FxRate(currency_code="EUR", rate_date=friday, rate_per_usd=Decimal("0.90000000"))
    )
    await db_session.commit()

    result = await convert(db_session, Decimal("100"), "USD", "EUR", sunday)
    assert result == Decimal("90.0000")
