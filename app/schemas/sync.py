"""Schemas for the two sync endpoints (POST/GET /api/v1/sync)."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas._validators import normalize_currency_code
from app.schemas.entitlement import EntitlementSignal
from app.schemas.enums import ParseStatus, SyncRecordStatus

MAX_PUSH_BATCH_SIZE = 500
DEFAULT_PULL_LIMIT = 200
MAX_PULL_LIMIT = 500


class ExpenseSyncIn(BaseModel):
    """One client-supplied record in a push batch.

    `user_id` is deliberately absent — it's always taken from the
    authenticated token, never trusted from the body. `device_id` is
    per-record (not per-request) because a batch synced after being offline
    for a while could span more than one device.
    """

    id: UUID
    device_id: UUID
    amount_original: Decimal
    currency_original: str = Field(min_length=3, max_length=3)
    spent_at: datetime
    client_rev: int = Field(ge=0)
    category: str | None = None
    payment_method: str | None = None
    merchant: str | None = None
    note: str | None = None
    raw_transcript: str | None = None
    deleted_at: datetime | None = None

    @field_validator("currency_original")
    @classmethod
    def _validate_currency_original(cls, value: str) -> str:
        return normalize_currency_code(value)


class SyncPushRequest(BaseModel):
    """`records` is intentionally untyped (raw dicts) rather than
    `list[ExpenseSyncIn]` — the endpoint validates each record individually
    against `ExpenseSyncIn` and rejects malformed ones on their own, so one
    bad record from a stale app version doesn't 422 an entire offline
    queue's worth of otherwise-valid syncs. The batch-size cap is still
    enforced here, at the whole-request level.
    """

    records: list[dict[str, Any]] = Field(max_length=MAX_PUSH_BATCH_SIZE)


class SyncPushResult(BaseModel):
    id: UUID
    status: SyncRecordStatus
    reason: str | None = None
    server_seq: int | None = None


class SyncPushResponse(BaseModel):
    results: list[SyncPushResult]
    server_high_water: int
    # Usage/upgrade-nudge signal — see app/services/metering/signal.py. Never
    # gates this endpoint; it's purely informational (the SACRED RULE: sync
    # always accepts and stores valid records regardless of quota).
    entitlement: EntitlementSignal


class ExpenseSyncOut(BaseModel):
    """Full server view of an expense, as returned by pull — including
    server-owned fields (server_seq, amount_base, parse_status) that later
    phases (FX normalization, LLM enrichment) populate. Soft-deleted rows
    (deleted_at set) are included as tombstones, not filtered out.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    device_id: UUID
    amount_original: Decimal
    currency_original: str
    amount_base: Decimal | None
    currency_base: str | None
    category: str | None
    payment_method: str | None
    merchant: str | None
    note: str | None
    spent_at: datetime
    created_at: datetime
    updated_at: datetime
    parse_status: ParseStatus
    raw_transcript: str | None
    client_rev: int
    deleted_at: datetime | None
    server_seq: int


class SyncPullResponse(BaseModel):
    records: list[ExpenseSyncOut]
    next_cursor: int
    has_more: bool
