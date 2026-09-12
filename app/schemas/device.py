from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.enums import Platform


class DeviceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    platform: Platform
    push_token: str | None
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime
