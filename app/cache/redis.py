"""Async Redis client factory and FastAPI dependency."""

from collections.abc import AsyncGenerator

from redis.asyncio import Redis

from app.core.config import get_settings

_client: Redis | None = None


def get_redis_client() -> Redis:
    global _client
    if _client is None:
        settings = get_settings()
        _client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


async def close_redis_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def get_redis() -> AsyncGenerator[Redis, None]:
    """FastAPI dependency yielding the shared async Redis client."""
    yield get_redis_client()
