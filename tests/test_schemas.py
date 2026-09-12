"""Tests for the Pydantic schemas — validators and ORM round-tripping."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, Expense, User
from app.schemas.expense import ExpenseCreate, ExpenseRead, ExpenseUpdate
from app.schemas.user import UserRead


def test_expense_create_normalizes_currency_case() -> None:
    payload = ExpenseCreate(
        id=uuid.uuid4(),
        amount_original=Decimal("10.00"),
        currency_original="usd",
        spent_at=datetime.now(UTC),
    )
    assert payload.currency_original == "USD"


def test_expense_create_rejects_bad_currency_length() -> None:
    with pytest.raises(ValidationError):
        ExpenseCreate(
            id=uuid.uuid4(),
            amount_original=Decimal("10.00"),
            currency_original="usdollar",
            spent_at=datetime.now(UTC),
        )


def test_expense_update_all_fields_optional() -> None:
    update = ExpenseUpdate()
    assert update.model_dump(exclude_unset=True) == {}


def test_expense_update_normalizes_currency_base() -> None:
    update = ExpenseUpdate(currency_base="eur")
    assert update.currency_base == "EUR"


async def test_expense_read_from_orm_attributes(
    db_session: AsyncSession, user: User, device: Device
) -> None:
    expense = Expense(
        user_id=user.id,
        device_id=device.id,
        amount_original=Decimal("42.00"),
        currency_original="USD",
        spent_at=datetime.now(UTC),
    )
    db_session.add(expense)
    await db_session.commit()

    read = ExpenseRead.model_validate(expense)
    assert read.amount_original == Decimal("42.00")
    assert read.parse_status == "local"


async def test_user_read_from_orm_attributes(db_session: AsyncSession, user: User) -> None:
    read = UserRead.model_validate(user)
    assert read.id == user.id
    assert read.base_currency == "USD"
