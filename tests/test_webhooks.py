"""Tests for POST /api/v1/webhooks/revenuecat.

Auth here is a static shared-secret Authorization header (constant-time
compared), not HMAC — see the module docstring in app/api/v1/webhooks.py.
No get_current_user involved; this is server-to-server.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db
from app.main import create_app
from app.models import Entitlement, ProcessedWebhookEvent, User
from app.schemas.enums import EntitlementStatus, Tier
from app.services.entitlements.resolver import resolve_entitlement

pytestmark = pytest.mark.pg

WEBHOOK_SECRET = "test-webhook-secret"


def _client(db_session: AsyncSession) -> AsyncClient:
    app = create_app()

    async def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _event(
    *,
    event_id: str,
    event_type: str,
    app_user_id: str,
    event_timestamp_ms: int,
    expiration_at_ms: int | None = None,
    product_id: str | None = "pro_monthly",
    store: str | None = "APP_STORE",
) -> dict:
    return {
        "event": {
            "id": event_id,
            "type": event_type,
            "app_user_id": app_user_id,
            "event_timestamp_ms": event_timestamp_ms,
            "expiration_at_ms": expiration_at_ms,
            "product_id": product_id,
            "store": store,
        }
    }


async def _entitlement(db_session: AsyncSession, user_id) -> Entitlement | None:
    result = await db_session.execute(select(Entitlement).where(Entitlement.user_id == user_id))
    return result.scalar_one_or_none()


async def test_missing_authorization_header_rejected(
    db_session: AsyncSession, user: User, monkeypatch
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("REVENUECAT_WEBHOOK_AUTH", WEBHOOK_SECRET)

    async with _client(db_session) as client:
        response = await client.post(
            "/api/v1/webhooks/revenuecat",
            json=_event(
                event_id="evt-1",
                event_type="INITIAL_PURCHASE",
                app_user_id=str(user.id),
                event_timestamp_ms=1000,
            ),
        )

    get_settings.cache_clear()
    assert response.status_code == 401


async def test_wrong_authorization_header_rejected(
    db_session: AsyncSession, user: User, monkeypatch
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("REVENUECAT_WEBHOOK_AUTH", WEBHOOK_SECRET)

    async with _client(db_session) as client:
        response = await client.post(
            "/api/v1/webhooks/revenuecat",
            headers={"Authorization": "wrong-secret"},
            json=_event(
                event_id="evt-1",
                event_type="INITIAL_PURCHASE",
                app_user_id=str(user.id),
                event_timestamp_ms=1000,
            ),
        )

    get_settings.cache_clear()
    assert response.status_code == 401


async def test_initial_purchase_activates_pro(
    db_session: AsyncSession, user: User, monkeypatch
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("REVENUECAT_WEBHOOK_AUTH", WEBHOOK_SECRET)
    expiration_ms = int((datetime.now(UTC) + timedelta(days=30)).timestamp() * 1000)

    async with _client(db_session) as client:
        response = await client.post(
            "/api/v1/webhooks/revenuecat",
            headers={"Authorization": WEBHOOK_SECRET},
            json=_event(
                event_id="evt-initial",
                event_type="INITIAL_PURCHASE",
                app_user_id=str(user.id),
                event_timestamp_ms=1000,
                expiration_at_ms=expiration_ms,
            ),
        )

    get_settings.cache_clear()
    assert response.status_code == 200
    entitlement = await _entitlement(db_session, user.id)
    assert entitlement is not None
    assert entitlement.tier == Tier.PRO
    assert entitlement.status == EntitlementStatus.ACTIVE
    assert entitlement.current_period_end is not None
    assert entitlement.last_event_ts_ms == 1000


async def test_duplicate_event_id_processed_once(
    db_session: AsyncSession, user: User, monkeypatch
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("REVENUECAT_WEBHOOK_AUTH", WEBHOOK_SECRET)
    expiration_ms = int((datetime.now(UTC) + timedelta(days=30)).timestamp() * 1000)
    event = _event(
        event_id="evt-dup",
        event_type="INITIAL_PURCHASE",
        app_user_id=str(user.id),
        event_timestamp_ms=1000,
        expiration_at_ms=expiration_ms,
    )

    async with _client(db_session) as client:
        first = await client.post(
            "/api/v1/webhooks/revenuecat",
            headers={"Authorization": WEBHOOK_SECRET},
            json=event,
        )
        # Redelivered with a later event_timestamp_ms and a different type —
        # if de-duplication worked only on unrelated ordering logic, this
        # would incorrectly look like a legitimate newer event.
        replay = dict(event)
        replay["event"] = {**event["event"], "event_timestamp_ms": 999999, "type": "CANCELLATION"}
        second = await client.post(
            "/api/v1/webhooks/revenuecat",
            headers={"Authorization": WEBHOOK_SECRET},
            json=replay,
        )

    get_settings.cache_clear()
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "already_processed"

    entitlement = await _entitlement(db_session, user.id)
    assert entitlement is not None
    assert entitlement.status == EntitlementStatus.ACTIVE  # NOT cancelled by the replay

    count = await db_session.execute(
        select(ProcessedWebhookEvent).where(ProcessedWebhookEvent.event_id == "evt-dup")
    )
    assert len(count.scalars().all()) == 1


async def test_out_of_order_older_event_ignored(
    db_session: AsyncSession, user: User, monkeypatch
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("REVENUECAT_WEBHOOK_AUTH", WEBHOOK_SECRET)

    async with _client(db_session) as client:
        await client.post(
            "/api/v1/webhooks/revenuecat",
            headers={"Authorization": WEBHOOK_SECRET},
            json=_event(
                event_id="evt-newer",
                event_type="INITIAL_PURCHASE",
                app_user_id=str(user.id),
                event_timestamp_ms=5000,
            ),
        )
        stale_response = await client.post(
            "/api/v1/webhooks/revenuecat",
            headers={"Authorization": WEBHOOK_SECRET},
            json=_event(
                event_id="evt-older",
                event_type="CANCELLATION",
                app_user_id=str(user.id),
                event_timestamp_ms=1000,  # older than the event above
            ),
        )

    get_settings.cache_clear()
    assert stale_response.status_code == 200
    assert stale_response.json()["status"] == "ignored_stale_event"
    entitlement = await _entitlement(db_session, user.id)
    assert entitlement is not None
    assert entitlement.status == EntitlementStatus.ACTIVE  # unchanged by the stale CANCELLATION


async def test_cancellation_keeps_access_until_period_end(
    db_session: AsyncSession, user: User, monkeypatch
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("REVENUECAT_WEBHOOK_AUTH", WEBHOOK_SECRET)
    expiration_ms = int((datetime.now(UTC) + timedelta(days=10)).timestamp() * 1000)

    async with _client(db_session) as client:
        await client.post(
            "/api/v1/webhooks/revenuecat",
            headers={"Authorization": WEBHOOK_SECRET},
            json=_event(
                event_id="evt-a",
                event_type="INITIAL_PURCHASE",
                app_user_id=str(user.id),
                event_timestamp_ms=1000,
                expiration_at_ms=expiration_ms,
            ),
        )
        await client.post(
            "/api/v1/webhooks/revenuecat",
            headers={"Authorization": WEBHOOK_SECRET},
            json=_event(
                event_id="evt-b",
                event_type="CANCELLATION",
                app_user_id=str(user.id),
                event_timestamp_ms=2000,
                expiration_at_ms=expiration_ms,
            ),
        )

    get_settings.cache_clear()
    entitlement_row = await _entitlement(db_session, user.id)
    assert entitlement_row is not None
    assert entitlement_row.status == EntitlementStatus.CANCELLED

    effective = await resolve_entitlement(db_session, user)
    assert effective.is_pro is True  # access persists until period_end


async def test_expiration_drops_to_free(db_session: AsyncSession, user: User, monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("REVENUECAT_WEBHOOK_AUTH", WEBHOOK_SECRET)

    async with _client(db_session) as client:
        await client.post(
            "/api/v1/webhooks/revenuecat",
            headers={"Authorization": WEBHOOK_SECRET},
            json=_event(
                event_id="evt-a",
                event_type="INITIAL_PURCHASE",
                app_user_id=str(user.id),
                event_timestamp_ms=1000,
                expiration_at_ms=int(datetime.now(UTC).timestamp() * 1000),
            ),
        )
        await client.post(
            "/api/v1/webhooks/revenuecat",
            headers={"Authorization": WEBHOOK_SECRET},
            json=_event(
                event_id="evt-b",
                event_type="EXPIRATION",
                app_user_id=str(user.id),
                event_timestamp_ms=2000,
            ),
        )

    get_settings.cache_clear()
    entitlement = await _entitlement(db_session, user.id)
    assert entitlement is not None
    assert entitlement.tier == Tier.FREE
    assert entitlement.status == EntitlementStatus.EXPIRED


async def test_unknown_app_user_id_ignored_but_audited(
    db_session: AsyncSession, monkeypatch
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("REVENUECAT_WEBHOOK_AUTH", WEBHOOK_SECRET)

    async with _client(db_session) as client:
        response = await client.post(
            "/api/v1/webhooks/revenuecat",
            headers={"Authorization": WEBHOOK_SECRET},
            json=_event(
                event_id="evt-anon",
                event_type="INITIAL_PURCHASE",
                app_user_id="$RCAnonymousID:deadbeef",
                event_timestamp_ms=1000,
            ),
        )

    get_settings.cache_clear()
    assert response.status_code == 200
    assert response.json()["status"] == "ignored_unmapped_user"

    audited = await db_session.execute(
        select(ProcessedWebhookEvent).where(ProcessedWebhookEvent.event_id == "evt-anon")
    )
    assert audited.scalar_one_or_none() is not None
