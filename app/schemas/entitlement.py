from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.enums import EntitlementStatus, Store, Tier


class EntitlementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    tier: Tier
    status: EntitlementStatus
    product_id: str | None
    store: Store | None
    revenuecat_app_user_id: str | None
    current_period_end: datetime | None
    created_at: datetime
    updated_at: datetime


class EntitlementSignal(BaseModel):
    """The usage/entitlement block echoed on sync push and parse responses.

    This is a *signal* for the client to decide whether to show an upgrade
    nudge — never an enforcement mechanism on its own, and never a channel
    for cost/token data (see the guardrail in the Phase 5 spec: never leak
    token counts or cost here).
    """

    tier: Tier
    status: EntitlementStatus
    period: str
    used: int
    quota: int | None
    over_quota: bool
