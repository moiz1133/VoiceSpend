from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class UsageCounterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    period: str
    log_count: int
    created_at: datetime
    updated_at: datetime
