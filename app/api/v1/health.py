"""Liveness and readiness health endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis import get_redis
from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    """Static liveness probe — process is up and serving requests."""
    return {"status": "ok"}


@router.get("/ready")
async def readiness(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, object]:
    """Readiness probe — checks Postgres and Redis are reachable."""
    checks: dict[str, str] = {}
    healthy = True

    try:
        await db.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001 - report failure, don't crash the probe
        checks["postgres"] = f"error: {exc}"
        healthy = False

    try:
        await redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001 - report failure, don't crash the probe
        checks["redis"] = f"error: {exc}"
        healthy = False

    response.status_code = (
        status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return {"status": "ok" if healthy else "unavailable", "checks": checks}
