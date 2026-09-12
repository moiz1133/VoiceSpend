import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDMixin
from app.models._constraints import enum_check
from app.schemas.enums import EntitlementStatus, Store, Tier

_TABLE = "entitlements"


class Entitlement(UUIDMixin, TimestampMixin, Base):
    """Current subscription state — one row per user. Fed by RevenueCat
    webhooks in Phase 5; no webhook handling lives here.
    """

    __tablename__ = _TABLE
    __table_args__ = (
        enum_check("tier", Tier),
        enum_check("status", EntitlementStatus),
        enum_check("store", Store),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    tier: Mapped[str] = mapped_column(
        String, nullable=False, default=Tier.FREE, server_default=Tier.FREE
    )
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default=EntitlementStatus.ACTIVE,
        server_default=EntitlementStatus.ACTIVE,
    )
    product_id: Mapped[str | None] = mapped_column(String, nullable=True)
    store: Mapped[str | None] = mapped_column(String, nullable=True)
    revenuecat_app_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
