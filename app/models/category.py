import uuid

from sqlalchemy import Boolean, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDMixin

_TABLE = "categories"


class Category(UUIDMixin, TimestampMixin, Base):
    """A plain UNIQUE(user_id, name) would not dedupe system rows, since
    Postgres treats NULL user_id as distinct on every row. Two partial
    unique indexes cover the user-owned and system-global cases separately.
    """

    __tablename__ = _TABLE
    __table_args__ = (
        Index(
            "uq_categories_user_id_name",
            "user_id",
            "name",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index(
            "uq_categories_name_system",
            "name",
            unique=True,
            postgresql_where=text("user_id IS NULL"),
        ),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    icon: Mapped[str | None] = mapped_column(String, nullable=True)
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
