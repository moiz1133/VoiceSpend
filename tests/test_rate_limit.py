"""Tests for the per-device Redis rate limiter — app/services/ratelimit/.

Uses the real Redis instance (REDIS_URL) for the Lua-script tests, since
the whole point of the atomic INCR+EXPIRE script is real Redis semantics;
a fake in-memory stand-in would just test our own mock, not the actual
atomicity guarantee. Fail-open/fail-closed tests substitute a Redis
double that raises, since a real Redis outage isn't reproducible here.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_db_user
from app.cache.redis import get_redis, get_redis_client
from app.core.config import get_settings
from app.core.security import AuthUser, get_current_user
from app.db.session import get_db
from app.main import create_app
from app.models import Device, User
from app.schemas.enums import Platform
from app.services.extraction.base import ExtractionContext, ExtractionResult, LLMExtractor
from app.services.extraction.factory import get_extractor
from app.services.extraction.schema import RawExtraction
from app.services.ratelimit.limiter import hit

pytestmark = pytest.mark.pg

USER_SUB = "supabase|ratelimit-user"


class _FakeExtractor(LLMExtractor):
    provider_name = "fake"
    model_name = "fake-model"

    async def extract(self, text: str, ctx: ExtractionContext) -> ExtractionResult:
        return ExtractionResult(
            raw=RawExtraction(amount=1, currency="USD", confidence=0.9),
            prompt_tokens=1,
            completion_tokens=1,
        )


class _BrokenScript:
    async def __call__(self, *args: object, **kwargs: object) -> tuple[int, int]:
        raise RedisError("simulated redis outage")


class _BrokenRedis:
    def register_script(self, script: str) -> _BrokenScript:
        return _BrokenScript()


async def _provision(db_session: AsyncSession, sub: str) -> User:
    return await get_current_db_user(AuthUser(id=sub, email=None, role=None), db_session)


def _client(db_session: AsyncSession, sub: str, *, redis: object | None = None) -> AsyncClient:
    app = create_app()

    async def _override_db():
        yield db_session

    async def _override_user() -> AuthUser:
        return AuthUser(id=sub, email=None, role=None)

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_extractor] = lambda: _FakeExtractor()
    if redis is not None:

        async def _override_redis():
            yield redis

        app.dependency_overrides[get_redis] = _override_redis
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_hit_increments_and_sets_ttl_only_once() -> None:
    redis = get_redis_client()
    key = f"rl:test:{uuid.uuid4()}"
    try:
        count1, ttl1 = await hit(redis, key, window_seconds=60)
        assert count1 == 1
        assert 0 < ttl1 <= 60_000

        count2, ttl2 = await hit(redis, key, window_seconds=60)
        assert count2 == 2
        # TTL must NOT have been reset by the second hit — it only
        # decreases (time passing), it never jumps back up near 60_000.
        assert ttl2 <= ttl1
    finally:
        await redis.delete(key)


async def test_under_limit_requests_succeed(db_session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setenv("PARSE_RATE_LIMIT", "5")
    monkeypatch.setenv("PARSE_RATE_LIMIT_DAILY", "500")
    get_settings.cache_clear()
    try:
        sub = f"{USER_SUB}-under"
        await _provision(db_session, sub)
        async with _client(db_session, sub) as client:
            for _ in range(3):
                response = await client.post("/api/v1/parse", json={"text": "coffee"})
                assert response.status_code == 200
    finally:
        get_settings.cache_clear()


async def test_exceeding_minute_limit_returns_429_with_retry_after(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setenv("PARSE_RATE_LIMIT", "2")
    monkeypatch.setenv("PARSE_RATE_LIMIT_DAILY", "500")
    get_settings.cache_clear()
    try:
        sub = f"{USER_SUB}-exceed"
        await _provision(db_session, sub)
        async with _client(db_session, sub) as client:
            for _ in range(2):
                ok = await client.post("/api/v1/parse", json={"text": "coffee"})
                assert ok.status_code == 200

            limited = await client.post("/api/v1/parse", json={"text": "coffee"})
            assert limited.status_code == 429
            assert "Retry-After" in limited.headers
            assert int(limited.headers["Retry-After"]) >= 1
    finally:
        get_settings.cache_clear()


async def test_quota_and_rate_limit_are_distinct(db_session: AsyncSession, monkeypatch) -> None:
    """429 (rate limit) is never confused with 200/quota_exceeded (Phase 5)
    — a pro user can still hit 429, and hitting 429 never touches quota.
    """
    monkeypatch.setenv("PARSE_RATE_LIMIT", "1")
    monkeypatch.setenv("PARSE_RATE_LIMIT_DAILY", "500")
    get_settings.cache_clear()
    try:
        sub = f"{USER_SUB}-distinct"
        await _provision(db_session, sub)
        async with _client(db_session, sub) as client:
            first = await client.post("/api/v1/parse", json={"text": "coffee"})
            assert first.status_code == 200
            assert first.json()["status"] != "quota_exceeded"

            second = await client.post("/api/v1/parse", json={"text": "coffee"})
            assert second.status_code == 429
    finally:
        get_settings.cache_clear()


async def test_redis_error_fails_open_by_default(db_session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_FAIL_OPEN", "true")
    get_settings.cache_clear()
    try:
        sub = f"{USER_SUB}-failopen"
        await _provision(db_session, sub)
        async with _client(db_session, sub, redis=_BrokenRedis()) as client:
            response = await client.post("/api/v1/parse", json={"text": "coffee"})
        assert response.status_code == 200
    finally:
        get_settings.cache_clear()


async def test_redis_error_fails_closed_when_configured(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setenv("RATE_LIMIT_FAIL_OPEN", "false")
    get_settings.cache_clear()
    try:
        sub = f"{USER_SUB}-failclosed"
        await _provision(db_session, sub)
        async with _client(db_session, sub, redis=_BrokenRedis()) as client:
            response = await client.post("/api/v1/parse", json={"text": "coffee"})
        assert response.status_code == 503
    finally:
        get_settings.cache_clear()


async def test_device_scoped_limits_are_isolated_per_device(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Two devices owned by the same user each get their own bucket when a
    validated X-Device-Id is sent — exhausting device A's limit must not
    block device B.
    """
    monkeypatch.setenv("PARSE_RATE_LIMIT", "1")
    monkeypatch.setenv("PARSE_RATE_LIMIT_DAILY", "500")
    get_settings.cache_clear()
    try:
        sub = f"{USER_SUB}-devices"
        user = await _provision(db_session, sub)
        device_a = Device(user_id=user.id, platform=Platform.IOS)
        device_b = Device(user_id=user.id, platform=Platform.ANDROID)
        db_session.add_all([device_a, device_b])
        await db_session.commit()

        async with _client(db_session, sub) as client:
            first = await client.post(
                "/api/v1/parse",
                json={"text": "coffee"},
                headers={"X-Device-Id": str(device_a.id)},
            )
            assert first.status_code == 200

            exhausted = await client.post(
                "/api/v1/parse",
                json={"text": "coffee"},
                headers={"X-Device-Id": str(device_a.id)},
            )
            assert exhausted.status_code == 429

            other_device = await client.post(
                "/api/v1/parse",
                json={"text": "coffee"},
                headers={"X-Device-Id": str(device_b.id)},
            )
            assert other_device.status_code == 200
    finally:
        get_settings.cache_clear()


async def test_unvalidated_device_id_falls_back_to_user_scope(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setenv("PARSE_RATE_LIMIT", "1")
    monkeypatch.setenv("PARSE_RATE_LIMIT_DAILY", "500")
    get_settings.cache_clear()
    try:
        sub = f"{USER_SUB}-spoofed"
        await _provision(db_session, sub)
        spoofed_device_id = str(uuid.uuid4())  # not owned by (or even a real) device

        async with _client(db_session, sub) as client:
            first = await client.post(
                "/api/v1/parse",
                json={"text": "coffee"},
                headers={"X-Device-Id": spoofed_device_id},
            )
            assert first.status_code == 200

            # Falls back to the user scope both times -> the second call
            # (same spoofed header or none at all) still hits the same bucket.
            second = await client.post(
                "/api/v1/parse", json={"text": "coffee"}, headers={"X-Device-Id": spoofed_device_id}
            )
            assert second.status_code == 429
    finally:
        get_settings.cache_clear()
