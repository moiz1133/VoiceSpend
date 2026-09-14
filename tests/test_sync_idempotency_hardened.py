"""Property/fuzz-style hardening for the sync push upsert — the highest-
risk invariant in the whole backend, since every other guarantee (quota
metering, amount_base recompute, client reconciliation) rides on it.

Real Postgres only: xmax, the ON CONFLICT WHERE clause, and the
server_seq trigger are all dialect-specific and would lie under SQLite.
"""

import asyncio
import random
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.v1.sync import push_sync
from app.models import Device, Expense, UsageCounter, User
from app.schemas.enums import Platform, SyncRecordStatus
from app.schemas.sync import SyncPushRequest
from app.services.metering.usage import current_period

pytestmark = pytest.mark.pg


def _record(device_id: uuid.UUID, *, id: uuid.UUID, client_rev: int, **overrides) -> dict:
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


async def _usage(db_session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await db_session.execute(
        select(UsageCounter.log_count).where(
            UsageCounter.user_id == user_id, UsageCounter.period == current_period()
        )
    )
    return result.scalar_one_or_none() or 0


async def _state_snapshot(db_session: AsyncSession, user_id: uuid.UUID) -> dict[uuid.UUID, dict]:
    rows = (
        await db_session.execute(select(Expense).where(Expense.user_id == user_id))
    ).scalars().all()
    return {
        row.id: {
            "server_seq": row.server_seq,
            "client_rev": row.client_rev,
            "amount_original": row.amount_original,
            "note": row.note,
            "deleted_at": row.deleted_at,
        }
        for row in rows
    }


@pytest.mark.parametrize("replay_count", [1, 2, 5, 10])
async def test_replaying_same_batch_n_times_is_byte_identical(
    db_session: AsyncSession, user: User, device: Device, replay_count: int
) -> None:
    ids = [uuid.uuid4() for _ in range(3)]
    batch = SyncPushRequest(
        records=[_record(device.id, id=i, client_rev=1, note="v1") for i in ids]
    )

    first = await push_sync(batch, user, db_session)
    assert all(r.status == SyncRecordStatus.APPLIED for r in first.results)
    snapshot_after_first = await _state_snapshot(db_session, user.id)
    usage_after_first = await _usage(db_session, user.id)
    assert usage_after_first == 3

    for _ in range(replay_count):
        replay = await push_sync(batch, user, db_session)
        assert all(r.status == SyncRecordStatus.STALE_IGNORED for r in replay.results)
        assert await _state_snapshot(db_session, user.id) == snapshot_after_first
        assert await _usage(db_session, user.id) == usage_after_first


async def test_randomized_rev_ordering_lww_holds_regardless_of_arrival_order(
    db_session: AsyncSession, user: User, device: Device
) -> None:
    record_id = uuid.uuid4()
    revs = [5, 1, 9, 3, 7, 2, 8]
    shuffled = revs.copy()
    random.shuffle(shuffled)

    for rev in shuffled:
        payload = SyncPushRequest(
            records=[_record(device.id, id=record_id, client_rev=rev, note=f"rev-{rev}")]
        )
        await push_sync(payload, user, db_session)

    stored = (
        await db_session.execute(select(Expense).where(Expense.id == record_id))
    ).scalar_one()
    assert stored.client_rev == max(revs)
    assert stored.note == f"rev-{max(revs)}"


async def test_soft_delete_replayed_among_edits_never_resurrects_or_double_counts(
    db_session: AsyncSession, user: User, device: Device
) -> None:
    record_id = uuid.uuid4()
    deleted_at = datetime.now(UTC).isoformat()

    create = SyncPushRequest(
        records=[_record(device.id, id=record_id, client_rev=1, note="alive")]
    )
    delete = SyncPushRequest(
        records=[
            _record(device.id, id=record_id, client_rev=2, note="alive", deleted_at=deleted_at)
        ]
    )

    await push_sync(create, user, db_session)
    first_delete = await push_sync(delete, user, db_session)
    assert first_delete.results[0].status == SyncRecordStatus.APPLIED

    # Replay the delete (and an even-older create) interleaved, several times.
    for _ in range(3):
        replay_delete = await push_sync(delete, user, db_session)
        assert replay_delete.results[0].status == SyncRecordStatus.STALE_IGNORED
        replay_create = await push_sync(create, user, db_session)
        assert replay_create.results[0].status == SyncRecordStatus.STALE_IGNORED

    stored = (
        await db_session.execute(select(Expense).where(Expense.id == record_id))
    ).scalar_one()
    assert stored.deleted_at is not None  # never resurrected
    assert await _usage(db_session, user.id) == 1  # only the original create counted


async def _push_with_fresh_session(
    db_engine: AsyncEngine, user_id: uuid.UUID, device_id: uuid.UUID, record_id: uuid.UUID
) -> SyncRecordStatus:
    """A genuinely independent session/connection — the shared `db_session`
    fixture wraps everything in one savepoint-nested transaction, which is
    not safe for concurrent use from multiple coroutines at once. Real
    concurrency needs real separate connections.
    """
    session = AsyncSession(bind=db_engine, expire_on_commit=False)
    try:
        user_obj = await session.get(User, user_id)
        assert user_obj is not None
        payload = SyncPushRequest(
            records=[_record(device_id, id=record_id, client_rev=1)]
        )
        response = await push_sync(payload, user_obj, session)
        return response.results[0].status
    finally:
        await session.close()


async def _make_real_committed_user_and_device(
    db_engine: AsyncEngine,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A genuinely committed (not savepoint-scoped) user/device — needed
    because the concurrency tests below open their own separate
    connections, which under READ COMMITTED can only see rows from a
    transaction that has actually committed at the top level. The shared
    `user`/`device` fixtures are deliberately savepoint-scoped and rolled
    back at test teardown, so they're invisible to a second connection.
    """
    session = AsyncSession(bind=db_engine, expire_on_commit=False)
    try:
        new_user = User()
        session.add(new_user)
        await session.commit()
        new_device = Device(user_id=new_user.id, platform=Platform.IOS)
        session.add(new_device)
        await session.commit()
        return new_user.id, new_device.id
    finally:
        await session.close()


async def _cleanup_real_user(db_engine: AsyncEngine, user_id: uuid.UUID) -> None:
    session = AsyncSession(bind=db_engine, expire_on_commit=False)
    try:
        real_user = await session.get(User, user_id)
        if real_user is not None:
            await session.delete(real_user)  # cascades to devices/expenses/usage_counters
            await session.commit()
    finally:
        await session.close()


async def test_concurrent_pushes_of_the_same_id_apply_exactly_once(
    db_engine: AsyncEngine,
) -> None:
    user_id, device_id = await _make_real_committed_user_and_device(db_engine)
    record_id = uuid.uuid4()

    try:
        statuses = await asyncio.gather(
            *[
                _push_with_fresh_session(db_engine, user_id, device_id, record_id)
                for _ in range(5)
            ]
        )

        assert statuses.count(SyncRecordStatus.APPLIED) == 1
        assert statuses.count(SyncRecordStatus.STALE_IGNORED) == 4

        cleanup = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            rows = (
                await cleanup.execute(select(Expense).where(Expense.id == record_id))
            ).scalars().all()
            assert len(rows) == 1  # exactly one row, never duplicated
            usage = await _usage(cleanup, user_id)
            assert usage == 1  # no lost updates, no double count
        finally:
            await cleanup.close()
    finally:
        await _cleanup_real_user(db_engine, user_id)


async def test_concurrent_pushes_of_distinct_new_ids_no_lost_updates(
    db_engine: AsyncEngine,
) -> None:
    user_id, device_id = await _make_real_committed_user_and_device(db_engine)
    record_ids = [uuid.uuid4() for _ in range(5)]

    try:
        statuses = await asyncio.gather(
            *[
                _push_with_fresh_session(db_engine, user_id, device_id, record_id)
                for record_id in record_ids
            ]
        )

        assert statuses == [SyncRecordStatus.APPLIED] * 5

        cleanup = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            rows = (
                await cleanup.execute(select(Expense).where(Expense.id.in_(record_ids)))
            ).scalars().all()
            server_seqs = [row.server_seq for row in rows]
            assert len(rows) == 5
            assert len(set(server_seqs)) == 5  # no duplicate server_seq assigned
            usage = await _usage(cleanup, user_id)
            assert usage == 5  # the atomic increment lost none of the 5 concurrent +1s
        finally:
            await cleanup.close()
    finally:
        await _cleanup_real_user(db_engine, user_id)
