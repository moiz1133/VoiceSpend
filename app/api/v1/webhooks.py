"""POST /api/v1/webhooks/revenuecat — server-to-server billing events.

AUTH: RevenueCat does not sign requests with HMAC. It sends a static
shared secret in the `Authorization` header that we configure in the
RevenueCat dashboard (`settings.REVENUECAT_WEBHOOK_AUTH`), compared with
`hmac.compare_digest` for constant-time matching. There is no
`get_current_user` here — this is server-to-server, not a user session.
When Stripe/Lemon Squeezy land later, they use real HMAC signature
verification — a different mechanism, and a separate handler.

IDEMPOTENCY + ORDERING: RevenueCat redelivers events and does not
guarantee delivery order, so this handler:
  1. Records every event in `processed_webhook_events` keyed by the
     provider's event id, via INSERT ... ON CONFLICT DO NOTHING. A redelivery
     inserts zero rows -> already processed -> return 200 and stop.
  2. Maps `app_user_id` to our internal user (the client sets RevenueCat's
     `appUserID` to our user UUID). An anonymous RC id or an id that
     doesn't match any user is recorded (for audit) but never touches
     entitlements.
  3. Skips the state change (but keeps the audit row) if the event is
     older than the last event that actually changed this user's row
     (`entitlements.last_event_ts_ms`).
  4. Otherwise applies the state transition and advances the watermark.

Always returns 200 on handled/ignored events — RevenueCat retries non-2xx
responses, so a 5xx here should mean "please retry," not "bad input."
"""

import hmac
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.metrics import record_webhook_event
from app.db.session import get_db
from app.models import Entitlement, ProcessedWebhookEvent, User
from app.schemas.enums import EntitlementStatus, Store, Tier
from app.schemas.webhook import RevenueCatEvent, RevenueCatWebhookPayload

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# event.type -> (tier, status) it puts the entitlements row into. Event
# types not listed here (e.g. TEST, TRANSFER, SUBSCRIPTION_PAUSED) are
# recorded for audit and otherwise ignored rather than guessed at.
_EVENT_STATE: dict[str, tuple[Tier, EntitlementStatus]] = {
    "INITIAL_PURCHASE": (Tier.PRO, EntitlementStatus.ACTIVE),
    "RENEWAL": (Tier.PRO, EntitlementStatus.ACTIVE),
    "PRODUCT_CHANGE": (Tier.PRO, EntitlementStatus.ACTIVE),
    "UNCANCELLATION": (Tier.PRO, EntitlementStatus.ACTIVE),
    "CANCELLATION": (Tier.PRO, EntitlementStatus.CANCELLED),
    "BILLING_ISSUE": (Tier.PRO, EntitlementStatus.IN_GRACE),
    "EXPIRATION": (Tier.FREE, EntitlementStatus.EXPIRED),
}

_STORE_MAP: dict[str, Store] = {
    "APP_STORE": Store.APP_STORE,
    "MAC_APP_STORE": Store.APP_STORE,
    "PLAY_STORE": Store.PLAY_STORE,
    "STRIPE": Store.STRIPE,
}


def _verify_auth(authorization: str | None) -> None:
    expected = get_settings().REVENUECAT_WEBHOOK_AUTH
    if not expected or authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid webhook credentials")


def _resolve_store(raw_store: str | None) -> Store | None:
    if raw_store is None:
        return None
    return _STORE_MAP.get(raw_store.upper())


def _resolve_period_end(expiration_at_ms: int | None) -> datetime | None:
    if expiration_at_ms is None:
        return None
    return datetime.fromtimestamp(expiration_at_ms / 1000, tz=UTC)


async def _apply_entitlement_transition(
    db: AsyncSession,
    *,
    user_id: UUID,
    event: RevenueCatEvent,
    tier: Tier,
    status: EntitlementStatus,
) -> None:
    store = _resolve_store(event.store)
    period_end = _resolve_period_end(event.expiration_at_ms)

    stmt = pg_insert(Entitlement).values(
        user_id=user_id,
        tier=tier.value,
        status=status.value,
        product_id=event.product_id,
        store=store.value if store else None,
        revenuecat_app_user_id=event.app_user_id,
        current_period_end=period_end,
        last_event_ts_ms=event.event_timestamp_ms,
    )
    update_cols: dict[str, object] = {
        "tier": stmt.excluded.tier,
        "status": stmt.excluded.status,
        "revenuecat_app_user_id": stmt.excluded.revenuecat_app_user_id,
        "last_event_ts_ms": stmt.excluded.last_event_ts_ms,
        "updated_at": func.now(),
    }
    # Only overwrite these when the event actually carries a value — some
    # event types (e.g. BILLING_ISSUE) may omit product_id/store, and we
    # don't want to null out a previously-known value just because this
    # particular event didn't repeat it.
    if event.product_id is not None:
        update_cols["product_id"] = stmt.excluded.product_id
    if store is not None:
        update_cols["store"] = stmt.excluded.store
    if period_end is not None:
        update_cols["current_period_end"] = stmt.excluded.current_period_end

    stmt = stmt.on_conflict_do_update(index_elements=[Entitlement.user_id], set_=update_cols)
    await db.execute(stmt)


@router.post("/revenuecat", status_code=200)
async def revenuecat_webhook(
    payload: RevenueCatWebhookPayload,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    _verify_auth(authorization)
    event = payload.event

    inserted = await db.execute(
        pg_insert(ProcessedWebhookEvent)
        .values(
            event_id=event.id,
            event_type=event.type,
            raw_payload=payload.model_dump(),
        )
        .on_conflict_do_nothing(index_elements=[ProcessedWebhookEvent.event_id])
        .returning(ProcessedWebhookEvent.event_id)
    )
    if inserted.first() is None:
        await db.commit()
        record_webhook_event(event_type=event.type, outcome="duplicate")
        return {"status": "already_processed"}

    try:
        user_id = UUID(event.app_user_id)
    except ValueError:
        await db.commit()
        record_webhook_event(event_type=event.type, outcome="unmatched")
        return {"status": "ignored_unmapped_user"}

    user_result = await db.execute(select(User.id).where(User.id == user_id))
    if user_result.scalar_one_or_none() is None:
        await db.commit()
        record_webhook_event(event_type=event.type, outcome="unmatched")
        return {"status": "ignored_unmapped_user"}

    entitlement_result = await db.execute(
        select(Entitlement).where(Entitlement.user_id == user_id)
    )
    entitlement = entitlement_result.scalar_one_or_none()
    if (
        entitlement is not None
        and entitlement.last_event_ts_ms is not None
        and event.event_timestamp_ms < entitlement.last_event_ts_ms
    ):
        await db.commit()
        record_webhook_event(event_type=event.type, outcome="stale")
        return {"status": "ignored_stale_event"}

    state = _EVENT_STATE.get(event.type)
    if state is None:
        await db.commit()
        record_webhook_event(event_type=event.type, outcome="unmatched")
        return {"status": "ignored_unhandled_event_type"}

    tier, status = state
    await _apply_entitlement_transition(
        db, user_id=user_id, event=event, tier=tier, status=status
    )
    await db.commit()
    record_webhook_event(event_type=event.type, outcome="applied")
    return {"status": "processed"}
