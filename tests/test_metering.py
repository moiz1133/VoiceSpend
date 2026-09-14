"""Tests for usage metering on POST /api/v1/sync — app/services/metering.

The core guarantee under test: counting a "log" against the free quota
rides on Phase 3's idempotent upsert via `(xmax = 0)` in the RETURNING
clause (see the docstring on push_sync in app/api/v1/sync.py). A genuinely
new expense row counts once; replays, edits, and soft deletes never do.
"""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_db_user
from app.core.security import AuthUser, get_current_user
from app.db.session import get_db
from app.main import create_app
from app.models import Device, UsageCounter, User
from app.schemas.enums import Platform
from app.services.metering.usage import current_period

pytestmark = pytest.mark.pg

USER_SUB = "supabase|metering-user"


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


def _record(device_id: uuid.UUID, *, id: uuid.UUID | None = None, client_rev: int = 0, **overrides):
    payload = {
        "id": str(id or uuid.uuid4()),
        "device_id": str(device_id),
        "amount_original": "12.50",
        "currency_original": "USD",
        "spent_at": datetime.now(UTC).isoformat(),
        "client_rev": client_rev,
    }
    payload.update(overrides)
    return payload


async def _usage(db_session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await db_session.execute(
        select(UsageCounter.log_count).where(
            UsageCounter.user_id == user_id, UsageCounter.period == current_period()
        )
    )
    return result.scalar_one_or_none() or 0


async def test_new_records_increment_counter(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_SUB)
    device = await _add_device(db_session, user)
    records = [_record(device.id) for _ in range(5)]

    async with _client_as(db_session, USER_SUB) as client:
        response = await client.post("/api/v1/sync", json={"records": records})

    assert response.status_code == 200
    assert await _usage(db_session, user.id) == 5
    assert response.json()["entitlement"]["used"] == 5
    assert response.json()["entitlement"]["quota"] == 20
    assert response.json()["entitlement"]["over_quota"] is False


async def test_replayed_batch_does_not_double_count(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_SUB)
    device = await _add_device(db_session, user)
    body = {"records": [_record(device.id, id=uuid.uuid4(), client_rev=1) for _ in range(3)]}

    async with _client_as(db_session, USER_SUB) as client:
        first = await client.post("/api/v1/sync", json=body)
        second = await client.post("/api/v1/sync", json=body)

    assert first.json()["entitlement"]["used"] == 3
    assert second.json()["entitlement"]["used"] == 3  # unchanged, not 6
    assert await _usage(db_session, user.id) == 3


async def test_edit_does_not_increment_counter(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_SUB)
    device = await _add_device(db_session, user)
    record_id = uuid.uuid4()

    async with _client_as(db_session, USER_SUB) as client:
        await client.post(
            "/api/v1/sync",
            json={"records": [_record(device.id, id=record_id, client_rev=1, note="v1")]},
        )
        edit = await client.post(
            "/api/v1/sync",
            json={"records": [_record(device.id, id=record_id, client_rev=2, note="v2")]},
        )

    assert edit.json()["results"][0]["status"] == "applied"
    assert edit.json()["entitlement"]["used"] == 1  # still just the one create
    assert await _usage(db_session, user.id) == 1


async def test_soft_delete_does_not_increment_counter(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_SUB)
    device = await _add_device(db_session, user)
    record_id = uuid.uuid4()

    async with _client_as(db_session, USER_SUB) as client:
        await client.post(
            "/api/v1/sync", json={"records": [_record(device.id, id=record_id, client_rev=1)]}
        )
        delete = await client.post(
            "/api/v1/sync",
            json={
                "records": [
                    _record(
                        device.id,
                        id=record_id,
                        client_rev=2,
                        deleted_at=datetime.now(UTC).isoformat(),
                    )
                ]
            },
        )

    assert delete.json()["results"][0]["status"] == "applied"
    assert delete.json()["entitlement"]["used"] == 1
    assert await _usage(db_session, user.id) == 1


async def test_overlapping_batches_count_each_new_id_once(db_session: AsyncSession) -> None:
    """Two pushes with overlapping ids (one shared, one new each) — the
    shared id must count once total, not once per batch it appears in.
    """
    user = await _provision(db_session, USER_SUB)
    device = await _add_device(db_session, user)
    shared_id = uuid.uuid4()

    async with _client_as(db_session, USER_SUB) as client:
        batch1 = await client.post(
            "/api/v1/sync",
            json={
                "records": [
                    _record(device.id, id=shared_id, client_rev=1),
                    _record(device.id, id=uuid.uuid4(), client_rev=1),
                ]
            },
        )
        # Replays shared_id (same client_rev -> stale_ignored, not counted
        # again) alongside one genuinely new id.
        batch2 = await client.post(
            "/api/v1/sync",
            json={
                "records": [
                    _record(device.id, id=shared_id, client_rev=1),
                    _record(device.id, id=uuid.uuid4(), client_rev=1),
                ]
            },
        )

    assert batch1.json()["entitlement"]["used"] == 2
    assert batch2.json()["entitlement"]["used"] == 3
    assert await _usage(db_session, user.id) == 3
