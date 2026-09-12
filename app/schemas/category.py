from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID | None
    name: str
    icon: str | None
    is_system: bool
    created_at: datetime
    updated_at: datetime
