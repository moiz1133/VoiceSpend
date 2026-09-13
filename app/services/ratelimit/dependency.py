"""FastAPI dependency enforcing the per-device parse rate limit.

Applied explicitly to POST /api/v1/parse and POST /api/v1/transcribe only
— see their route definitions in app/api/v1/parse.py. Never wired as
global middleware: /api/v1/sync (logging must never be blocked) and the
RevenueCat webhook (server-to-server, not a per-device caller) must never
be rate limited, and the only way to guarantee that is to attach this
dependency to exactly the two endpoints that cost us money per call.
"""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_db_user
from app.cache.redis import get_redis
from app.core.config import get_settings
from app.core.metrics import record_rate_limited, record_rate_limiter_redis_error
from app.db.session import get_db
from app.models import Device, User
from app.services.ratelimit.limiter import hit

logger = logging.getLogger(__name__)

_MINUTE = 60
_DAY = 86_400


async def _resolve_scope(request: Request, user: User, db: AsyncSession) -> str:
    """device_id when the caller sent a valid X-Device-Id header that
    genuinely belongs to them (reusing the same ownership check
    app/api/v1/sync.py applies to push records) — an unvalidated device id
    is never trusted as a rate-limit scope, since that would let one user
    exhaust another user's device-scoped limit by guessing/spoofing an id.
    Falls back to the user_id otherwise.
    """
    raw_device_id = request.headers.get("x-device-id")
    if raw_device_id:
        try:
            device_id = UUID(raw_device_id)
        except ValueError:
            device_id = None
        if device_id is not None:
            owned = await db.execute(
                select(Device.id).where(Device.id == device_id, Device.user_id == user.id)
            )
            if owned.scalar_one_or_none() is not None:
                return f"device:{device_id}"
    return f"user:{user.id}"


def _endpoint_label(request: Request) -> str:
    # Bounded: only ever "parse" or "transcribe" for the two routes this
    # dependency is attached to.
    return request.url.path.rsplit("/", 1)[-1]


async def enforce_parse_rate_limit(
    request: Request,
    user: Annotated[User, Depends(get_current_db_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> None:
    settings = get_settings()
    scope = await _resolve_scope(request, user, db)
    endpoint = _endpoint_label(request)

    windows = ((_MINUTE, settings.PARSE_RATE_LIMIT), (_DAY, settings.PARSE_RATE_LIMIT_DAILY))

    try:
        decisions: list[tuple[int, int, int]] = []
        for window_seconds, limit in windows:
            key = f"rl:parse:{scope}:{window_seconds}"
            count, ttl_ms = await hit(redis, key, window_seconds=window_seconds)
            decisions.append((count, ttl_ms, limit))
    except RedisError as exc:
        record_rate_limiter_redis_error()
        logger.error("rate limiter redis error, scope=%s: %s", scope, exc)
        if settings.RATE_LIMIT_FAIL_OPEN:
            return
        raise HTTPException(
            status_code=503, detail="rate limiter temporarily unavailable"
        ) from exc

    for count, ttl_ms, limit in decisions:
        if count > limit:
            retry_after = max(1, (ttl_ms + 999) // 1000)
            record_rate_limited(endpoint)
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded, slow down",
                headers={"Retry-After": str(retry_after)},
            )
