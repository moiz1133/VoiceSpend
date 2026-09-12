"""The offline sync endpoints: POST /api/v1/sync (push) and GET /api/v1/sync
(pull). See app/models/expense.py and the server_seq migration for the
cursor mechanism these rely on.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import AuthUser, get_current_user
from app.db.session import get_db
from app.models import Device, Expense, User
from app.schemas.enums import SyncRecordStatus
from app.schemas.sync import (
    DEFAULT_PULL_LIMIT,
    MAX_PULL_LIMIT,
    ExpenseSyncIn,
    ExpenseSyncOut,
    SyncPullResponse,
    SyncPushRequest,
    SyncPushResponse,
    SyncPushResult,
)

router = APIRouter(prefix="/sync", tags=["sync"])

# Columns the client may set on an upsert. device_id is intentionally
# excluded from updates (only used on insert) — it records which device
# *created* the expense and doesn't change on later edits from elsewhere.
_CLIENT_UPDATE_COLUMNS = (
    "amount_original",
    "currency_original",
    "category",
    "payment_method",
    "merchant",
    "note",
    "spent_at",
    "client_rev",
    "raw_transcript",
    "deleted_at",
)


async def get_current_db_user(
    auth_user: Annotated[AuthUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Resolve the verified Supabase identity to our internal `users` row,
    provisioning one on first sight — this is what makes anonymous/
    device-first sign-in work: the JWT `sub` is stable from the first
    request, so the local user row it maps to is created lazily rather
    than requiring a separate signup step.
    """
    existing = await db.execute(select(User).where(User.auth_provider_id == auth_user.id))
    user = existing.scalar_one_or_none()
    if user is not None:
        return user

    user = User(
        auth_provider_id=auth_user.id,
        email=auth_user.email,
        is_anonymous=auth_user.email is None,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # Lost a race with a concurrent request for the same auth_user.id.
        await db.rollback()
        existing = await db.execute(select(User).where(User.auth_provider_id == auth_user.id))
        user = existing.scalar_one()
    else:
        await db.refresh(user)
    return user


async def _high_water(db: AsyncSession, user_id: UUID) -> int:
    result = await db.execute(
        select(func.max(Expense.server_seq)).where(Expense.user_id == user_id)
    )
    return result.scalar() or 0


def _first_error_reason(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "invalid record"
    first = errors[0]
    loc = ".".join(str(part) for part in first["loc"])
    return f"{loc}: {first['msg']}" if loc else str(first["msg"])


@router.post("", response_model=SyncPushResponse)
async def push_sync(
    payload: SyncPushRequest,
    user: Annotated[User, Depends(get_current_db_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SyncPushResponse:
    """PUSH: idempotent record-level-LWW upsert of a batch of expenses.

    Validation happens per record (malformed/unauthorized records are
    rejected individually, not the whole batch) so one bad record from a
    stale app version can't block sync for the rest of an offline queue.
    The actual upsert is a single bulk INSERT ... ON CONFLICT DO UPDATE —
    never a per-record loop — so it stays one round trip and the WHERE on
    the DO UPDATE makes replaying the same batch a true no-op.
    """
    rejects: dict[UUID, str] = {}
    parsed: dict[UUID, ExpenseSyncIn] = {}
    order: list[UUID] = []

    for raw in payload.records:
        raw_id = raw.get("id") if isinstance(raw, dict) else None
        try:
            record = ExpenseSyncIn.model_validate(raw)
        except ValidationError as exc:
            try:
                record_id = UUID(str(raw_id))
            except (TypeError, ValueError):
                continue  # no usable id to key a result on — nothing to report
            if record_id not in rejects and record_id not in parsed:
                rejects[record_id] = _first_error_reason(exc)
                order.append(record_id)
            continue

        if record.id in parsed or record.id in rejects:
            rejects.setdefault(record.id, "duplicate id in batch")
            continue

        parsed[record.id] = record
        order.append(record.id)

    valid_records: list[ExpenseSyncIn] = []
    if parsed:
        device_ids = {r.device_id for r in parsed.values()}
        owned_device_ids = set(
            (
                await db.execute(
                    select(Device.id).where(Device.id.in_(device_ids), Device.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )

        owner_rows = await db.execute(
            select(Expense.id, Expense.user_id).where(Expense.id.in_(parsed.keys()))
        )
        existing_owners: dict[UUID, UUID] = {row.id: row.user_id for row in owner_rows.all()}

        for record in parsed.values():
            if record.device_id not in owned_device_ids:
                rejects[record.id] = "device_id not found or not owned by user"
                continue
            owner = existing_owners.get(record.id)
            if owner is not None and owner != user.id:
                rejects[record.id] = "record id belongs to a different user"
                continue
            valid_records.append(record)

    applied: dict[UUID, int] = {}
    if valid_records:
        stmt = pg_insert(Expense).values(
            [
                {
                    "id": r.id,
                    "user_id": user.id,
                    "device_id": r.device_id,
                    "amount_original": r.amount_original,
                    "currency_original": r.currency_original,
                    "category": r.category,
                    "payment_method": r.payment_method,
                    "merchant": r.merchant,
                    "note": r.note,
                    "spent_at": r.spent_at,
                    "client_rev": r.client_rev,
                    "raw_transcript": r.raw_transcript,
                    "deleted_at": r.deleted_at,
                }
                for r in valid_records
            ]
        )
        upsert_stmt = stmt.on_conflict_do_update(
            index_elements=[Expense.id],
            set_={col: getattr(stmt.excluded, col) for col in _CLIENT_UPDATE_COLUMNS},
            where=Expense.client_rev < stmt.excluded.client_rev,
        ).returning(Expense.id, Expense.server_seq)

        result = await db.execute(upsert_stmt)
        applied = {row.id: row.server_seq for row in result.all()}
        await db.commit()

    results = []
    for record_id in order:
        if record_id in rejects:
            results.append(
                SyncPushResult(
                    id=record_id, status=SyncRecordStatus.REJECTED, reason=rejects[record_id]
                )
            )
        elif record_id in applied:
            results.append(
                SyncPushResult(
                    id=record_id,
                    status=SyncRecordStatus.APPLIED,
                    server_seq=applied[record_id],
                )
            )
        else:
            results.append(SyncPushResult(id=record_id, status=SyncRecordStatus.STALE_IGNORED))

    return SyncPushResponse(results=results, server_high_water=await _high_water(db, user.id))


@router.get("", response_model=SyncPullResponse)
async def pull_sync(
    user: Annotated[User, Depends(get_current_db_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    since: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_PULL_LIMIT)] = DEFAULT_PULL_LIMIT,
) -> SyncPullResponse:
    """PULL: everything changed after `since`, ascending by server_seq.

    Tombstones (deleted_at set) and the caller's own already-synced rows
    are included deliberately — that's how a soft delete or a server-side
    enrichment (FX, LLM parsing in later phases) propagates back to other
    devices. `since=0` (the default) means a full sync from scratch.
    """
    result = await db.execute(
        select(Expense)
        .where(Expense.user_id == user.id, Expense.server_seq > since)
        .order_by(Expense.server_seq.asc())
        .limit(limit)
    )
    rows = result.scalars().all()
    records = [ExpenseSyncOut.model_validate(row) for row in rows]
    next_cursor = records[-1].server_seq if records else since
    return SyncPullResponse(
        records=records, next_cursor=next_cursor, has_more=len(records) == limit
    )
