from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas._validators import normalize_currency_code
from app.schemas.enums import ParseStatus


class ExpenseRead(BaseModel):
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


class ExpenseCreate(BaseModel):
    """What the client supplies when logging an expense. `id` is the
    client-generated idempotency key — retried syncs of the same logical
    expense must send the same `id`. `user_id`/`device_id` are attributed
    server-side from the authenticated session, not sent by the client.
    """

    id: UUID
    amount_original: Decimal
    currency_original: str = Field(min_length=3, max_length=3)
    spent_at: datetime
    client_rev: int = 0
    raw_transcript: str | None = None
    category: str | None = None
    payment_method: str | None = None
    merchant: str | None = None
    note: str | None = None

    @field_validator("currency_original")
    @classmethod
    def _validate_currency_original(cls, value: str) -> str:
        return normalize_currency_code(value)


class ExpenseUpdate(BaseModel):
    """Partial update — e.g. server-side FX enrichment or a client edit
    syncing in. Only fields present in the payload should be applied
    (use `.model_dump(exclude_unset=True)` at the call site).
    """

    amount_original: Decimal | None = None
    currency_original: str | None = Field(default=None, min_length=3, max_length=3)
    amount_base: Decimal | None = None
    currency_base: str | None = Field(default=None, min_length=3, max_length=3)
    category: str | None = None
    payment_method: str | None = None
    merchant: str | None = None
    note: str | None = None
    spent_at: datetime | None = None
    parse_status: ParseStatus | None = None
    raw_transcript: str | None = None
    client_rev: int | None = None
    deleted_at: datetime | None = None

    @field_validator("currency_original", "currency_base")
    @classmethod
    def _validate_currency(cls, value: str | None) -> str | None:
        return normalize_currency_code(value) if value is not None else None
