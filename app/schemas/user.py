from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    auth_provider_id: str | None
    email: str | None
    is_anonymous: bool
    base_currency: str
    created_at: datetime
    updated_at: datetime
