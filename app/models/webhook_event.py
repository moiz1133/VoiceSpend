from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_TABLE = "processed_webhook_events"


class ProcessedWebhookEvent(Base):
    """Exactly-once + audit log for inbound billing webhooks (RevenueCat now,
    Stripe/Lemon Squeezy later). `event_id` as the primary key is what makes
    processing idempotent: a redelivered event's INSERT ... ON CONFLICT DO
    NOTHING affects zero rows, which is how the handler recognizes a replay
    and stops without touching entitlements again.
    """

    __tablename__ = _TABLE

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
