"""Tests for the entitlement resolver — app/services/entitlements/resolver.py."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Entitlement, User
from app.schemas.enums import EntitlementStatus, Tier
from app.services.entitlements.resolver import resolve_entitlement

pytestmark = pytest.mark.pg

async def test_no_entitlement_row_resolves_to_free(db_session: AsyncSession, user: User) -> None:
    effective = await resolve_entitlement(db_session, user)
    assert effective.tier == Tier.FREE
    assert effective.status == EntitlementStatus.ACTIVE
    assert effective.is_pro is False


async def test_active_pro_resolves_to_is_pro(db_session: AsyncSession, user: User) -> None:
    db_session.add(
        Entitlement(
            user_id=user.id,
            tier=Tier.PRO,
            status=EntitlementStatus.ACTIVE,
            current_period_end=datetime.now(UTC) + timedelta(days=30),
        )
    )
    await db_session.commit()

    effective = await resolve_entitlement(db_session, user)
    assert effective.is_pro is True
    assert effective.tier == Tier.PRO
    assert effective.status == EntitlementStatus.ACTIVE


async def test_cancelled_before_period_end_still_is_pro(
    db_session: AsyncSession, user: User
) -> None:
    db_session.add(
        Entitlement(
            user_id=user.id,
            tier=Tier.PRO,
            status=EntitlementStatus.CANCELLED,
            current_period_end=datetime.now(UTC) + timedelta(days=5),
        )
    )
    await db_session.commit()

    effective = await resolve_entitlement(db_session, user)
    assert effective.is_pro is True
    assert effective.status == EntitlementStatus.CANCELLED


async def test_expired_status_resolves_to_free(db_session: AsyncSession, user: User) -> None:
    db_session.add(
        Entitlement(
            user_id=user.id,
            tier=Tier.PRO,
            status=EntitlementStatus.EXPIRED,
            current_period_end=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await db_session.commit()

    effective = await resolve_entitlement(db_session, user)
    assert effective.is_pro is False
    assert effective.status == EntitlementStatus.EXPIRED


async def test_period_end_in_past_resolves_to_free_even_if_status_active(
    db_session: AsyncSession, user: User
) -> None:
    db_session.add(
        Entitlement(
            user_id=user.id,
            tier=Tier.PRO,
            status=EntitlementStatus.ACTIVE,
            current_period_end=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await db_session.commit()

    effective = await resolve_entitlement(db_session, user)
    assert effective.is_pro is False


async def test_in_grace_is_pro(db_session: AsyncSession, user: User) -> None:
    db_session.add(
        Entitlement(
            user_id=user.id,
            tier=Tier.PRO,
            status=EntitlementStatus.IN_GRACE,
            current_period_end=datetime.now(UTC) + timedelta(days=3),
        )
    )
    await db_session.commit()

    effective = await resolve_entitlement(db_session, user)
    assert effective.is_pro is True
    assert effective.status == EntitlementStatus.IN_GRACE
