import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CHAR, BigInteger, DateTime, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDMixin
from app.models._constraints import enum_check
from app.schemas.enums import ParseStatus

_TABLE = "expenses"


class Expense(UUIDMixin, TimestampMixin, Base):
    """The idempotency-key-as-PK pattern: `id` is client-generated (see
    UUIDMixin — the uuid4 default only kicks in if the caller omits it),
    so a retried offline sync of the same logical expense collapses onto
    the same row instead of duplicating.
    """

    __tablename__ = _TABLE
    __table_args__ = (enum_check("parse_status", ParseStatus),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False
    )

    amount_original: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency_original: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    amount_base: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    currency_base: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)

    category: Mapped[str | None] = mapped_column(String, nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String, nullable=True)
    merchant: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    spent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    parse_status: Mapped[str] = mapped_column(
        String, nullable=False, default=ParseStatus.LOCAL, server_default=ParseStatus.LOCAL
    )
    raw_transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_rev: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Defined post-class (rather than in __table_args__) so `.desc()` can be used
# for the dashboard's "most recent first" access pattern.
Index("ix_expenses_user_id_spent_at", Expense.user_id, Expense.spent_at.desc())
Index("ix_expenses_user_id_updated_at", Expense.user_id, Expense.updated_at)
