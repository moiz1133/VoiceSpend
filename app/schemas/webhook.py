"""Inbound payload shape for POST /api/v1/webhooks/revenuecat.

Deliberately lenient (`extra="ignore"`, most fields optional): RevenueCat
adds fields to this payload over time and we only need a handful of them.
Only the fields the handler actually branches on are required.
"""

from pydantic import BaseModel, ConfigDict


class RevenueCatEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    type: str
    app_user_id: str
    event_timestamp_ms: int
    product_id: str | None = None
    store: str | None = None
    expiration_at_ms: int | None = None


class RevenueCatWebhookPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event: RevenueCatEvent
