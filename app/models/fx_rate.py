from datetime import date
from decimal import Decimal

from sqlalchemy import CHAR, Date, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDMixin

_TABLE = "fx_rates"


class FxRate(UUIDMixin, TimestampMixin, Base):
    """Historical daily rates, USD-pivot. Cross rates between two non-USD
    currencies are computed at query time (rate_a / rate_b) — no cross-pair
    rows are stored.
    """

    __tablename__ = _TABLE
    __table_args__ = (
        UniqueConstraint(
            "currency_code", "rate_date", name="uq_fx_rates_currency_code_rate_date"
        ),
    )

    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    rate_per_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    source: Mapped[str | None] = mapped_column(String, nullable=True)


Index("ix_fx_rates_currency_code_rate_date", FxRate.currency_code, FxRate.rate_date.desc())
