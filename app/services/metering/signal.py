"""Builds the `entitlement` block echoed on sync push and parse responses —
the one piece of client-visible state that says "you're at N/quota, go
upgrade." Composes the entitlement resolver with the usage counter so
callers (app/api/v1/sync.py, app/api/v1/parse.py) don't have to.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import User
from app.schemas.entitlement import EntitlementSignal
from app.services.entitlements.resolver import resolve_entitlement
from app.services.metering.usage import current_period, get_usage_count


async def build_entitlement_signal(db: AsyncSession, user: User) -> EntitlementSignal:
    effective = await resolve_entitlement(db, user)
    period = current_period()
    used = await get_usage_count(db, user.id, period)

    if effective.is_pro:
        quota: int | None = None
        over_quota = False
    else:
        quota = get_settings().FREE_MONTHLY_LOG_QUOTA
        over_quota = used >= quota

    return EntitlementSignal(
        tier=effective.tier,
        status=effective.status,
        period=period,
        used=used,
        quota=quota,
        over_quota=over_quota,
    )
