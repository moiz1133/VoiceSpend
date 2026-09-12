from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.enums import EntitlementStatus, Store, Tier


class EntitlementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    tier: Tier
    status: EntitlementStatus
    product_id: str | None
    store: Store | None
    revenuecat_app_user_id: str | None
    current_period_end: datetime | None
    created_at: datetime
    updated_at: datetime
