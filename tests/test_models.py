"""Round-trip tests for the Phase 2 data model against a real Postgres.

Run with a Postgres reachable at $DATABASE_URL (default: the docker-compose
service — `docker compose up -d postgres`). These exercise things sqlite
can't: Numeric precision, CHAR(3), partial unique indexes, CHECK
constraints, and FK ON DELETE behavior.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, Device, Entitlement, Expense, FxRate, UsageCounter, User
from app.schemas.enums import ParseStatus, Platform

pytestmark = pytest.mark.pg

async def test_user_roundtrip_and_defaults(db_session: AsyncSession) -> None:
    user = User(email="alice@example.com", is_anonymous=False)
    db_session.add(user)
    await db_session.commit()

    fetched = await db_session.get(User, user.id)
    assert fetched is not None
    assert fetched.email == "alice@example.com"
    assert fetched.is_anonymous is False
    assert fetched.base_currency == "USD"  # default
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


async def test_device_roundtrip_and_platform_check(
    db_session: AsyncSession, user: User
) -> None:
    device = Device(user_id=user.id, platform=Platform.ANDROID, push_token="tok-123")
    db_session.add(device)
    await db_session.commit()

    fetched = await db_session.get(Device, device.id)
    assert fetched is not None
    assert fetched.platform == "android"
    assert fetched.push_token == "tok-123"

    db_session.add(Device(user_id=user.id, platform="bogus"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_expense_roundtrip_with_client_generated_id(
    db_session: AsyncSession, user: User, device: Device
) -> None:
    client_id = uuid.uuid4()
    expense = Expense(
        id=client_id,
        user_id=user.id,
        device_id=device.id,
        amount_original=Decimal("12.3456"),
        currency_original="usd",  # intentionally lowercase; model stores as-is
        spent_at=datetime.now(UTC),
    )
    db_session.add(expense)
    await db_session.commit()

    fetched = await db_session.get(Expense, client_id)
    assert fetched is not None
    assert fetched.id == client_id  # caller-supplied id was respected, not regenerated
    assert fetched.amount_original == Decimal("12.3456")
    assert fetched.parse_status == ParseStatus.LOCAL
    assert fetched.client_rev == 0
    assert fetched.deleted_at is None
    assert fetched.amount_base is None


async def test_expense_id_defaults_when_omitted(
    db_session: AsyncSession, user: User, device: Device
) -> None:
    expense = Expense(
        user_id=user.id,
        device_id=device.id,
        amount_original=Decimal("5.00"),
        currency_original="EUR",
        spent_at=datetime.now(UTC),
    )
    db_session.add(expense)
    await db_session.commit()

    assert isinstance(expense.id, uuid.UUID)


async def test_expense_invalid_parse_status_rejected(
    db_session: AsyncSession, user: User, device: Device
) -> None:
    expense = Expense(
        user_id=user.id,
        device_id=device.id,
        amount_original=Decimal("1.00"),
        currency_original="USD",
        spent_at=datetime.now(UTC),
        parse_status="not-a-real-status",
    )
    db_session.add(expense)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_device_restrict_blocks_delete_while_expense_exists(
    db_session: AsyncSession, user: User, device: Device
) -> None:
    expense = Expense(
        user_id=user.id,
        device_id=device.id,
        amount_original=Decimal("1.00"),
        currency_original="USD",
        spent_at=datetime.now(UTC),
    )
    db_session.add(expense)
    await db_session.commit()

    await db_session.delete(device)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_user_delete_cascades_devices_and_expenses(
    db_session: AsyncSession, user: User, device: Device
) -> None:
    expense = Expense(
        user_id=user.id,
        device_id=device.id,
        amount_original=Decimal("1.00"),
        currency_original="USD",
        spent_at=datetime.now(UTC),
    )
    db_session.add(expense)
    await db_session.commit()
    expense_id, device_id = expense.id, device.id

    await db_session.delete(user)
    await db_session.commit()

    # `.get()` would short-circuit on the (stale) identity map without
    # hitting the DB; `select()` always issues real SQL, correctly
    # reflecting the DB-level ON DELETE CASCADE.
    assert (
        await db_session.execute(select(Expense).where(Expense.id == expense_id))
    ).scalar_one_or_none() is None
    assert (
        await db_session.execute(select(Device).where(Device.id == device_id))
    ).scalar_one_or_none() is None


async def test_category_partial_unique_indexes(db_session: AsyncSession, user: User) -> None:
    # Captured up front: `rollback()` expires every object in the session,
    # and re-fetching an expired attribute is a sync lazy-load, which
    # AsyncSession forbids outside an explicit await.
    user_id = user.id

    # Deliberately not "Food"/"Transport"/etc — the real migration seeds
    # those 8 system categories, and the test DB is now built by running
    # that migration (see conftest.db_engine), not bare metadata.create_all.
    name = "ZzzTestOnlyCategory"

    db_session.add(Category(user_id=None, name=name, is_system=True))
    await db_session.commit()

    # A second system category with the same name violates the NULL-scoped
    # partial unique index.
    db_session.add(Category(user_id=None, name=name, is_system=True))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    # A user-owned category with the same name is a different partial index
    # scope, so it's allowed alongside the system one.
    db_session.add(Category(user_id=user_id, name=name))
    await db_session.commit()

    # But not duplicated for the same user.
    db_session.add(Category(user_id=user_id, name=name))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_fx_rate_roundtrip_and_unique_per_day(db_session: AsyncSession) -> None:
    rate = FxRate(
        currency_code="EUR",
        rate_date=date(2026, 1, 1),
        rate_per_usd=Decimal("0.92000000"),
        source="ecb",
    )
    db_session.add(rate)
    await db_session.commit()

    result = await db_session.execute(select(FxRate).where(FxRate.currency_code == "EUR"))
    fetched = result.scalar_one()
    assert fetched.rate_per_usd == Decimal("0.92000000")

    dup_rate = FxRate(currency_code="EUR", rate_date=date(2026, 1, 1), rate_per_usd=Decimal("1"))
    db_session.add(dup_rate)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_entitlement_roundtrip_and_one_per_user(
    db_session: AsyncSession, user: User
) -> None:
    entitlement = Entitlement(user_id=user.id)
    db_session.add(entitlement)
    await db_session.commit()

    fetched = await db_session.get(Entitlement, entitlement.id)
    assert fetched is not None
    assert fetched.tier == "free"
    assert fetched.status == "active"

    db_session.add(Entitlement(user_id=user.id))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_usage_counter_roundtrip_and_unique_per_period(
    db_session: AsyncSession, user: User
) -> None:
    counter = UsageCounter(user_id=user.id, period="2026-09")
    db_session.add(counter)
    await db_session.commit()

    fetched = await db_session.get(UsageCounter, counter.id)
    assert fetched is not None
    assert fetched.log_count == 0

    db_session.add(UsageCounter(user_id=user.id, period="2026-09"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
