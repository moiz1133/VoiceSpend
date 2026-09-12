import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDMixin

_TABLE = "usage_counters"


class UsageCounter(UUIDMixin, TimestampMixin, Base):
    """Freemium metering — one row per (user, 'YYYY-MM' period)."""

    __tablename__ = _TABLE
    __table_args__ = (
        UniqueConstraint("user_id", "period", name="uq_usage_counters_user_id_period"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    period: Mapped[str] = mapped_column(String, nullable=False)
    log_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
