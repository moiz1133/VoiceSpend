"""Tests for the Phase 6 sync-upsert staleness tweak: editing
amount_original/currency_original/spent_at must null amount_base +
currency_base (re-arming the row for the next recompute sweep), while an
edit that touches only a client-owned display field must NOT.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_db_user
from app.core.security import AuthUser, get_current_user
from app.db.session import get_db
from app.main import create_app
from app.models import Device, Expense, User
from app.schemas.enums import Platform

pytestmark = pytest.mark.pg

USER_SUB = "supabase|staleness-user"


async def _provision(db_session: AsyncSession, sub: str) -> User:
    return await get_current_db_user(AuthUser(id=sub, email=None, role=None), db_session)


async def _add_device(db_session: AsyncSession, owner: User) -> Device:
    device = Device(user_id=owner.id, platform=Platform.IOS)
    db_session.add(device)
    await db_session.commit()
    return device


def _client_as(db_session: AsyncSession, sub: str) -> AsyncClient:
    app = create_app()

    async def _override_user() -> AuthUser:
        return AuthUser(id=sub, email=None, role=None)

    async def _override_db():
        yield db_session

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db] = _override_db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _record(device_id: uuid.UUID, *, id: uuid.UUID, client_rev: int, **overrides):
    payload = {
        "id": str(id),
        "device_id": str(device_id),
        "amount_original": "12.50",
        "currency_original": "USD",
        "spent_at": datetime.now(UTC).isoformat(),
        "client_rev": client_rev,
    }
    payload.update(overrides)
    return payload


async def _seed_enriched(
    db_session: AsyncSession, record_id: uuid.UUID, amount_base: Decimal, currency_base: str
) -> None:
    """Stands in for a prior recompute sweep having already filled these in."""
    await db_session.execute(
        update(Expense)
        .where(Expense.id == record_id)
        .values(amount_base=amount_base, currency_base=currency_base)
    )
    await db_session.commit()


async def test_editing_amount_nulls_amount_base(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_SUB)
    device = await _add_device(db_session, user)
    record_id = uuid.uuid4()

    async with _client_as(db_session, USER_SUB) as client:
        await client.post(
            "/api/v1/sync",
            json={"records": [_record(device.id, id=record_id, client_rev=1)]},
        )
        await _seed_enriched(db_session, record_id, Decimal("12.5000"), "USD")

        edit = await client.post(
            "/api/v1/sync",
            json={
                "records": [
                    _record(
                        device.id, id=record_id, client_rev=2, amount_original="20.00"
                    )
                ]
            },
        )

    assert edit.json()["results"][0]["status"] == "applied"
    stored = (
        await db_session.execute(select(Expense).where(Expense.id == record_id))
    ).scalar_one()
    assert stored.amount_original == Decimal("20.00")
    assert stored.amount_base is None
    assert stored.currency_base is None


async def test_editing_currency_nulls_amount_base(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_SUB)
    device = await _add_device(db_session, user)
    record_id = uuid.uuid4()

    async with _client_as(db_session, USER_SUB) as client:
        await client.post(
            "/api/v1/sync",
            json={"records": [_record(device.id, id=record_id, client_rev=1)]},
        )
        await _seed_enriched(db_session, record_id, Decimal("12.5000"), "USD")

        edit = await client.post(
            "/api/v1/sync",
            json={
                "records": [
                    _record(device.id, id=record_id, client_rev=2, currency_original="EUR")
                ]
            },
        )

    assert edit.json()["results"][0]["status"] == "applied"
    stored = (
        await db_session.execute(select(Expense).where(Expense.id == record_id))
    ).scalar_one()
    assert stored.amount_base is None
    assert stored.currency_base is None


async def test_editing_spent_at_nulls_amount_base(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_SUB)
    device = await _add_device(db_session, user)
    record_id = uuid.uuid4()
    original_spent_at = datetime(2026, 1, 1, tzinfo=UTC)
    new_spent_at = datetime(2026, 2, 1, tzinfo=UTC)

    async with _client_as(db_session, USER_SUB) as client:
        await client.post(
            "/api/v1/sync",
            json={
                "records": [
                    _record(
                        device.id,
                        id=record_id,
                        client_rev=1,
                        spent_at=original_spent_at.isoformat(),
                    )
                ]
            },
        )
        await _seed_enriched(db_session, record_id, Decimal("12.5000"), "USD")

        edit = await client.post(
            "/api/v1/sync",
            json={
                "records": [
                    _record(
                        device.id, id=record_id, client_rev=2, spent_at=new_spent_at.isoformat()
                    )
                ]
            },
        )

    assert edit.json()["results"][0]["status"] == "applied"
    stored = (
        await db_session.execute(select(Expense).where(Expense.id == record_id))
    ).scalar_one()
    assert stored.amount_base is None
    assert stored.currency_base is None


async def test_editing_only_note_preserves_amount_base(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_SUB)
    device = await _add_device(db_session, user)
    record_id = uuid.uuid4()

    # spent_at is pinned explicitly and reused for both pushes — _record()'s
    # default recomputes datetime.now(UTC) on every call, which would make
    # spent_at itself "change" between pushes and null amount_base for the
    # wrong reason, defeating the point of this test.
    spent_at = datetime.now(UTC).isoformat()

    async with _client_as(db_session, USER_SUB) as client:
        await client.post(
            "/api/v1/sync",
            json={
                "records": [
                    _record(device.id, id=record_id, client_rev=1, spent_at=spent_at)
                ]
            },
        )
        await _seed_enriched(db_session, record_id, Decimal("12.5000"), "USD")

        edit = await client.post(
            "/api/v1/sync",
            json={
                "records": [
                    _record(
                        device.id,
                        id=record_id,
                        client_rev=2,
                        spent_at=spent_at,
                        note="just a note edit",
                    )
                ]
            },
        )

    assert edit.json()["results"][0]["status"] == "applied"
    stored = (
        await db_session.execute(select(Expense).where(Expense.id == record_id))
    ).scalar_one()
    assert stored.note == "just a note edit"
    assert stored.amount_base == Decimal("12.5000")
    assert stored.currency_base == "USD"
