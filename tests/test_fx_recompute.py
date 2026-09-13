"""Tests for the amount_base NULL-filling sweep — app/services/fx/recompute.py.

The key thing under test beyond "the math is right": writes go through a
plain SQL UPDATE, so the expenses.server_seq trigger fires and the row
becomes pullable past a previously-seen cursor — that's what makes
enrichment actually propagate to other devices.
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, Expense, FxRate, User
from app.services.fx.recompute import null_amount_base_for_user, recompute_amount_base


async def _make_expense(
    db_session: AsyncSession,
    owner: User,
    device: Device,
    *,
    amount: Decimal = Decimal("10.00"),
    currency: str = "EUR",
    amount_base: Decimal | None = None,
) -> Expense:
    expense = Expense(
        user_id=owner.id,
        device_id=device.id,
        amount_original=amount,
        currency_original=currency,
        amount_base=amount_base,
        spent_at=datetime.now(UTC),
    )
    db_session.add(expense)
    await db_session.commit()
    await db_session.refresh(expense)
    return expense


async def test_recompute_fills_amount_base_and_advances_server_seq(
    db_session: AsyncSession, user: User, device: Device
) -> None:
    db_session.add(
        FxRate(currency_code="EUR", rate_date=datetime.now(UTC).date(), rate_per_usd=Decimal("0.9"))
    )
    await db_session.commit()

    expense = await _make_expense(db_session, user, device, amount=Decimal("9.00"), currency="EUR")
    expense_id = expense.id  # captured before expiry — see below
    old_seq = expense.server_seq
    base_currency = user.base_currency

    result = await recompute_amount_base(db_session, user_id=user.id)

    assert result.scanned == 1
    assert result.recomputed == 1
    assert result.still_missing == 0

    # recompute writes via a Core-level UPDATE, which SQLAlchemy's ORM
    # session synchronizes into already-loaded objects only for the columns
    # named in .values() — server_seq is DB-trigger-owned and never
    # appears there, so the identity-mapped `expense` object's cached
    # server_seq would look stale without an explicit expire. expire_all()
    # marks every attribute (including `id`) for a lazy reload on next
    # access, which must happen inside an awaited call — hence using the
    # pre-captured expense_id in the query rather than expense.id here.
    db_session.expire_all()
    refreshed = (
        await db_session.execute(select(Expense).where(Expense.id == expense_id))
    ).scalar_one()
    assert refreshed.amount_base == Decimal("10.0000")  # 9 EUR / 0.9 = 10 USD
    assert refreshed.currency_base == base_currency
    assert refreshed.server_seq > old_seq  # trigger fired -> pullable past the old cursor


async def test_recompute_leaves_still_missing_rate_rows_null(
    db_session: AsyncSession, user: User, device: Device
) -> None:
    expense = await _make_expense(db_session, user, device, currency="XYZ")  # no rate seeded

    result = await recompute_amount_base(db_session, user_id=user.id)

    assert result.scanned == 1
    assert result.recomputed == 0
    assert result.still_missing == 1

    refreshed = (
        await db_session.execute(select(Expense).where(Expense.id == expense.id))
    ).scalar_one()
    assert refreshed.amount_base is None
    assert refreshed.currency_base is None


async def test_recompute_ignores_rows_that_already_have_amount_base(
    db_session: AsyncSession, user: User, device: Device
) -> None:
    await _make_expense(db_session, user, device, amount_base=Decimal("5.0000"))

    result = await recompute_amount_base(db_session, user_id=user.id)

    assert result.scanned == 0


async def test_recompute_ignores_soft_deleted_rows(
    db_session: AsyncSession, user: User, device: Device
) -> None:
    expense = await _make_expense(db_session, user, device, currency="EUR")
    db_session.add(
        FxRate(currency_code="EUR", rate_date=datetime.now(UTC).date(), rate_per_usd=Decimal("0.9"))
    )
    expense.deleted_at = datetime.now(UTC)
    db_session.add(expense)
    await db_session.commit()

    result = await recompute_amount_base(db_session, user_id=user.id)

    assert result.scanned == 0


async def test_null_amount_base_for_user_rearms_all_rows(
    db_session: AsyncSession, user: User, device: Device
) -> None:
    e1 = await _make_expense(db_session, user, device, amount_base=Decimal("5.0000"))
    e2 = await _make_expense(db_session, user, device, amount_base=Decimal("7.0000"))

    count = await null_amount_base_for_user(db_session, user.id)
    assert count == 2

    rows = (
        await db_session.execute(select(Expense).where(Expense.id.in_([e1.id, e2.id])))
    ).scalars().all()
    assert all(row.amount_base is None and row.currency_base is None for row in rows)
