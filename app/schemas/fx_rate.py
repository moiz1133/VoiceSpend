from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class FxRateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    currency_code: str
    rate_date: date
    rate_per_usd: Decimal
    source: str | None
    created_at: datetime
    updated_at: datetime
