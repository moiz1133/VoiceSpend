"""Tests for the sync push/pull endpoints — the crux of offline-first.

Auth is stubbed by overriding `get_current_user` per test-client with a
fixed `AuthUser`, so tests don't need real Supabase JWTs; `get_db` is
overridden to share the test's transactional `db_session` (from
tests/conftest.py) so HTTP calls and assertions see the same uncommitted
state, rolled back at teardown.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_db_user
from app.core.security import AuthUser, get_current_user
from app.db.session import get_db
from app.main import create_app
from app.models import Device, Expense, User
from app.schemas.enums import Platform

USER_A_SUB = "supabase|user-a"
USER_B_SUB = "supabase|user-b"


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


async def test_push_new_records_applied_with_increasing_server_seq(
    db_session: AsyncSession,
) -> None:
    user = await _provision(db_session, USER_A_SUB)
    device = await _add_device(db_session, user)

    async with _client_as(db_session, USER_A_SUB) as client:
        r1 = await client.post("/api/v1/sync", json={"records": [_record(device.id)]})
        r2 = await client.post("/api/v1/sync", json={"records": [_record(device.id)]})

    assert r1.status_code == 200
    assert r2.status_code == 200
    seq1 = r1.json()["results"][0]["server_seq"]
    seq2 = r2.json()["results"][0]["server_seq"]
    assert r1.json()["results"][0]["status"] == "applied"
    assert r2.json()["results"][0]["status"] == "applied"
    assert seq1 is not None and seq2 is not None
    assert seq2 > seq1

    count = await db_session.execute(select(Expense).where(Expense.user_id == user.id))
    assert len(count.scalars().all()) == 2


async def test_push_same_batch_twice_is_idempotent(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_A_SUB)
    device = await _add_device(db_session, user)
    record_id = uuid.uuid4()
    body = {"records": [_record(device.id, id=record_id, client_rev=1)]}

    async with _client_as(db_session, USER_A_SUB) as client:
        first = await client.post("/api/v1/sync", json=body)
        assert first.json()["results"][0]["status"] == "applied"
        first_seq = first.json()["results"][0]["server_seq"]

        second = await client.post("/api/v1/sync", json=body)

    assert second.status_code == 200
    assert second.json()["results"][0]["status"] == "stale_ignored"
    assert second.json()["results"][0]["server_seq"] is None

    stored = (
        await db_session.execute(select(Expense).where(Expense.id == record_id))
    ).scalar_one()
    assert stored.server_seq == first_seq  # trigger did NOT fire — byte-identical row
    assert stored.amount_original == Decimal("12.50")


async def test_push_lower_client_rev_is_ignored(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_A_SUB)
    device = await _add_device(db_session, user)
    record_id = uuid.uuid4()

    async with _client_as(db_session, USER_A_SUB) as client:
        await client.post(
            "/api/v1/sync",
            json={"records": [_record(device.id, id=record_id, client_rev=5, note="v5")]},
        )
        stale = await client.post(
            "/api/v1/sync",
            json={"records": [_record(device.id, id=record_id, client_rev=2, note="v2-stale")]},
        )

    assert stale.json()["results"][0]["status"] == "stale_ignored"
    stored = (
        await db_session.execute(select(Expense).where(Expense.id == record_id))
    ).scalar_one()
    assert stored.client_rev == 5
    assert stored.note == "v5"


async def test_push_higher_client_rev_applies_and_advances_seq(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_A_SUB)
    device = await _add_device(db_session, user)
    record_id = uuid.uuid4()

    async with _client_as(db_session, USER_A_SUB) as client:
        first = await client.post(
            "/api/v1/sync",
            json={"records": [_record(device.id, id=record_id, client_rev=1, note="v1")]},
        )
        second = await client.post(
            "/api/v1/sync",
            json={"records": [_record(device.id, id=record_id, client_rev=2, note="v2")]},
        )

    assert second.json()["results"][0]["status"] == "applied"
    stored = (
        await db_session.execute(select(Expense).where(Expense.id == record_id))
    ).scalar_one()
    assert stored.note == "v2"
    assert stored.client_rev == 2
    assert stored.server_seq > first.json()["results"][0]["server_seq"]


async def test_soft_delete_syncs_and_pull_returns_tombstone(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_A_SUB)
    device = await _add_device(db_session, user)
    record_id = uuid.uuid4()

    async with _client_as(db_session, USER_A_SUB) as client:
        await client.post(
            "/api/v1/sync", json={"records": [_record(device.id, id=record_id, client_rev=1)]}
        )
        delete_push = await client.post(
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
        pulled = await client.get("/api/v1/sync", params={"since": 0})

    assert delete_push.json()["results"][0]["status"] == "applied"
    tombstone = next(r for r in pulled.json()["records"] if r["id"] == str(record_id))
    assert tombstone["deleted_at"] is not None


async def test_pull_pagination_ascending_and_has_more(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_A_SUB)
    device = await _add_device(db_session, user)

    async with _client_as(db_session, USER_A_SUB) as client:
        for _ in range(5):
            await client.post("/api/v1/sync", json={"records": [_record(device.id)]})

        page1 = (await client.get("/api/v1/sync", params={"since": 0, "limit": 2})).json()
        page2 = (
            await client.get(
                "/api/v1/sync", params={"since": page1["next_cursor"], "limit": 2}
            )
        ).json()
        page3 = (
            await client.get(
                "/api/v1/sync", params={"since": page2["next_cursor"], "limit": 2}
            )
        ).json()

    assert [r["server_seq"] for r in page1["records"]] == sorted(
        r["server_seq"] for r in page1["records"]
    )
    assert page1["has_more"] is True
    assert page2["has_more"] is True
    assert len(page3["records"]) == 1
    assert page3["has_more"] is False
    all_seqs = [r["server_seq"] for r in page1["records"] + page2["records"] + page3["records"]]
    assert all_seqs == sorted(all_seqs)
    assert len(set(all_seqs)) == 5


async def test_cross_user_cannot_push_to_another_users_record(db_session: AsyncSession) -> None:
    user_a = await _provision(db_session, USER_A_SUB)
    device_a = await _add_device(db_session, user_a)
    user_b = await _provision(db_session, USER_B_SUB)
    device_b = await _add_device(db_session, user_b)
    record_id = uuid.uuid4()

    async with _client_as(db_session, USER_A_SUB) as client_a:
        await client_a.post(
            "/api/v1/sync",
            json={"records": [_record(device_a.id, id=record_id, client_rev=1, note="a-owns-it")]},
        )

    # B has their own valid device — the rejection here must specifically be
    # about record ownership, not (as a different test covers) device
    # ownership.
    async with _client_as(db_session, USER_B_SUB) as client_b:
        hijack = await client_b.post(
            "/api/v1/sync",
            json={
                "records": [
                    _record(device_b.id, id=record_id, client_rev=99, note="b-hijack")
                ]
            },
        )

    result = hijack.json()["results"][0]
    assert result["status"] == "rejected"
    assert "different user" in result["reason"]

    stored = (
        await db_session.execute(select(Expense).where(Expense.id == record_id))
    ).scalar_one()
    assert stored.note == "a-owns-it"
    assert stored.user_id == user_a.id


async def test_cross_user_pull_never_returns_other_users_rows(db_session: AsyncSession) -> None:
    user_a = await _provision(db_session, USER_A_SUB)
    device_a = await _add_device(db_session, user_a)
    user_b = await _provision(db_session, USER_B_SUB)
    await _add_device(db_session, user_b)

    async with _client_as(db_session, USER_A_SUB) as client_a:
        await client_a.post("/api/v1/sync", json={"records": [_record(device_a.id)]})

    async with _client_as(db_session, USER_B_SUB) as client_b:
        pulled_b = await client_b.get("/api/v1/sync", params={"since": 0})

    assert pulled_b.json()["records"] == []


async def test_unknown_device_id_rejected(db_session: AsyncSession) -> None:
    await _provision(db_session, USER_A_SUB)

    async with _client_as(db_session, USER_A_SUB) as client:
        response = await client.post(
            "/api/v1/sync", json={"records": [_record(uuid.uuid4())]}
        )

    result = response.json()["results"][0]
    assert result["status"] == "rejected"
    assert "device_id" in result["reason"]


async def test_other_users_device_id_rejected(db_session: AsyncSession) -> None:
    await _provision(db_session, USER_A_SUB)
    user_b = await _provision(db_session, USER_B_SUB)
    device_b = await _add_device(db_session, user_b)

    async with _client_as(db_session, USER_A_SUB) as client_a:
        response = await client_a.post(
            "/api/v1/sync", json={"records": [_record(device_b.id)]}
        )

    result = response.json()["results"][0]
    assert result["status"] == "rejected"
    assert "device_id" in result["reason"]


async def test_batch_over_500_rejected(db_session: AsyncSession) -> None:
    await _provision(db_session, USER_A_SUB)

    async with _client_as(db_session, USER_A_SUB) as client:
        response = await client.post(
            "/api/v1/sync", json={"records": [{} for _ in range(501)]}
        )

    assert response.status_code == 422


async def test_missing_required_field_rejects_only_that_record(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_A_SUB)
    device = await _add_device(db_session, user)
    good_id = uuid.uuid4()
    bad_id = uuid.uuid4()

    async with _client_as(db_session, USER_A_SUB) as client:
        response = await client.post(
            "/api/v1/sync",
            json={
                "records": [
                    _record(device.id, id=good_id),
                    {"id": str(bad_id), "device_id": str(device.id)},  # missing required fields
                ]
            },
        )

    assert response.status_code == 200
    results = {r["id"]: r for r in response.json()["results"]}
    assert results[str(good_id)]["status"] == "applied"
    assert results[str(bad_id)]["status"] == "rejected"


async def test_enrichment_write_bumps_server_seq_and_is_pullable(db_session: AsyncSession) -> None:
    """Proves the trigger fires for ANY write path, not just the sync
    upsert — a plain server-side UPDATE (standing in for Phase 4/6's FX/LLM
    enrichment) must still advance the cursor so other devices re-pull it.
    """
    user = await _provision(db_session, USER_A_SUB)
    device = await _add_device(db_session, user)
    record_id = uuid.uuid4()

    async with _client_as(db_session, USER_A_SUB) as client:
        pushed = await client.post(
            "/api/v1/sync", json={"records": [_record(device.id, id=record_id)]}
        )
        old_seq = pushed.json()["results"][0]["server_seq"]

        await db_session.execute(
            update(Expense).where(Expense.id == record_id).values(amount_base=Decimal("11.05"))
        )
        await db_session.commit()

        pulled = await client.get("/api/v1/sync", params={"since": old_seq})

    assert len(pulled.json()["records"]) == 1
    record = pulled.json()["records"][0]
    assert record["id"] == str(record_id)
    assert record["server_seq"] > old_seq
    assert Decimal(record["amount_base"]) == Decimal("11.05")
