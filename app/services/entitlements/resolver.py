"""Resolves a user's *effective* entitlement from their raw `entitlements`
row — the one place that decides what "is_pro" actually means, so callers
(parse gating, the sync response signal) never re-derive this logic.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Entitlement, User
from app.schemas.enums import EntitlementStatus, Tier

# Statuses that still carry active access. `cancelled` belongs here
# deliberately — a cancelled subscription keeps access until
# current_period_end, it just won't renew.
_ACCESS_RETAINING_STATUSES = frozenset(
    {EntitlementStatus.ACTIVE, EntitlementStatus.IN_GRACE, EntitlementStatus.CANCELLED}
)


@dataclass(frozen=True)
class EffectiveEntitlement:
    tier: Tier
    status: EntitlementStatus
    is_pro: bool


async def resolve_entitlement(db: AsyncSession, user: User) -> EffectiveEntitlement:
    """No entitlements row -> free/active (the common case: nobody has ever
    bought anything). Otherwise, pro access requires tier='pro', a status
    that still retains access, AND a period that hasn't lapsed — an expired
    period_end or status='expired' always falls back to free, regardless of
    what `tier` still says on the row (kept as-is so the client can still
    show "your Pro plan expired" rather than losing that fact).
    """
    result = await db.execute(select(Entitlement).where(Entitlement.user_id == user.id))
    entitlement = result.scalar_one_or_none()

    if entitlement is None:
        return EffectiveEntitlement(
            tier=Tier.FREE, status=EntitlementStatus.ACTIVE, is_pro=False
        )

    tier = Tier(entitlement.tier)
    status = EntitlementStatus(entitlement.status)
    period_not_lapsed = (
        entitlement.current_period_end is None
        or entitlement.current_period_end > datetime.now(UTC)
    )
    is_pro = tier == Tier.PRO and status in _ACCESS_RETAINING_STATUSES and period_not_lapsed

    return EffectiveEntitlement(tier=tier, status=status, is_pro=is_pro)
