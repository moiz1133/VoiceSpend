"""Shared test fixtures.

Phase 0 keeps this simple: health checks are exercised against the real
FastAPI app with the DB/Redis dependencies swapped for lightweight fakes, so
tests don't require Docker/Postgres/Redis to be running.
"""

import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/test")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

from app.cache.redis import get_redis  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import create_app  # noqa: E402


class FakeResult:
    def scalar_one(self) -> int:
        return 1


class FakeSession:
    """Stand-in for AsyncSession; raise_on_execute simulates a DB outage."""

    def __init__(self, raise_on_execute: bool = False) -> None:
        self.raise_on_execute = raise_on_execute

    async def execute(self, *_args: object, **_kwargs: object) -> FakeResult:
        if self.raise_on_execute:
            raise ConnectionError("simulated postgres outage")
        return FakeResult()


class FakeRedis:
    """Stand-in for the async Redis client; raise_on_ping simulates an outage."""

    def __init__(self, raise_on_ping: bool = False) -> None:
        self.raise_on_ping = raise_on_ping

    async def ping(self) -> bool:
        if self.raise_on_ping:
            raise ConnectionError("simulated redis outage")
        return True


@pytest.fixture
def app_healthy():
    app = create_app()

    async def db_override() -> AsyncGenerator[FakeSession, None]:
        yield FakeSession()

    async def redis_override() -> AsyncGenerator[FakeRedis, None]:
        yield FakeRedis()

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_redis] = redis_override
    return app


@pytest.fixture
def app_unhealthy():
    app = create_app()

    async def db_override() -> AsyncGenerator[FakeSession, None]:
        yield FakeSession(raise_on_execute=True)

    async def redis_override() -> AsyncGenerator[FakeRedis, None]:
        yield FakeRedis(raise_on_ping=True)

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_redis] = redis_override
    return app


@pytest_asyncio.fixture
async def healthy_client(app_healthy) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app_healthy)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def unhealthy_client(app_unhealthy) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app_unhealthy)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
