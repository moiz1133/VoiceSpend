"""The offline sync endpoints: POST /api/v1/sync (push) and GET /api/v1/sync
(pull). See app/models/expense.py and the server_seq migration for the
cursor mechanism these rely on.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import ValidationError
from sqlalchemy import case, func, literal_column, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_db_user
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
from app.services.metering.signal import build_entitlement_signal
from app.services.metering.usage import current_period, increment_usage

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

    Metering (Phase 5) rides on that same idempotency for free: RETURNING
    `(xmax = 0)` tells us, per returned row, whether Postgres actually
    INSERTed it (xmax = 0) versus reached it via the DO UPDATE path. Only
    genuinely new rows count against the free-tier monthly quota — a
    replayed batch returns no rows for already-applied records (they fail
    the client_rev WHERE) so it can never double-count, and an edit (higher
    client_rev on an existing row) returns was_inserted=false so it isn't
    counted either. This endpoint NEVER rejects a record for being over
    quota — metering here is count-and-signal only, never a gate.

    Phase 6 staleness tweak: amount_base/currency_base are a pure function
    of (amount_original, currency_original, spent_at, base_currency). If an
    edit changes any of the first three, the previously-stored amount_base
    is now wrong — and since Phase 6's recompute sweep only ever looks at
    rows where amount_base IS NULL, a stale non-NULL value would never get
    revisited. So the same DO UPDATE also nulls amount_base/currency_base
    whenever one of those columns is actually changing, re-arming the row
    for the next sweep. This is computed with a SQL CASE in the same
    statement, not a separate code path or a second query.
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
        # A change to any of these three invalidates the stored amount_base
        # (see the staleness-tweak docstring above) — this is a SQLAlchemy
        # ColumnElement, evaluated per-row inside the single UPDATE, not a
        # Python-side check (there is no Python-side "old row" to check here).
        amount_base_is_stale = or_(
            Expense.amount_original != stmt.excluded.amount_original,
            Expense.currency_original != stmt.excluded.currency_original,
            Expense.spent_at != stmt.excluded.spent_at,
        )
        set_clause: dict[str, Any] = {
            col: getattr(stmt.excluded, col) for col in _CLIENT_UPDATE_COLUMNS
        }
        set_clause["amount_base"] = case((amount_base_is_stale, None), else_=Expense.amount_base)
        set_clause["currency_base"] = case(
            (amount_base_is_stale, None), else_=Expense.currency_base
        )

        # Explicit `Any`: mixing an ORM column with a raw literal_column() in
        # .returning() defeats SQLAlchemy's normal type inference here.
        upsert_stmt: Any = stmt.on_conflict_do_update(
            index_elements=[Expense.id],
            set_=set_clause,
            where=Expense.client_rev < stmt.excluded.client_rev,
        ).returning(
            Expense.id, Expense.server_seq, literal_column("(xmax = 0)").label("was_inserted")
        )

        result = await db.execute(upsert_stmt)
        rows = result.all()
        applied = {row.id: row.server_seq for row in rows}
        new_count = sum(1 for row in rows if row.was_inserted)
        if new_count:
            await increment_usage(db, user.id, current_period(), new_count)
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

    return SyncPushResponse(
        results=results,
        server_high_water=await _high_water(db, user.id),
        entitlement=await build_entitlement_signal(db, user),
    )


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
