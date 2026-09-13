"""Shared test fixtures.

Phase 0 keeps this simple: health checks are exercised against the real
FastAPI app with the DB/Redis dependencies swapped for lightweight fakes, so
tests don't require Docker/Postgres/Redis to be running.
"""

import asyncio
import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from alembic import command

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://voicespend:voicespend@localhost:5432/voicespend"
)
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

import app.models  # noqa: E402, F401 — registers all models on Base.metadata
from app.cache.redis import get_redis  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Device, User  # noqa: E402
from app.schemas.enums import Platform  # noqa: E402
from app.worker.celery_app import celery_app  # noqa: E402

# Tests call Celery tasks (app.worker.tasks.fx) directly or via .delay() —
# eager mode runs them synchronously in-process instead of publishing to a
# real broker, so the test suite never needs Redis reachable for this.
celery_app.conf.task_always_eager = True
celery_app.conf.task_eager_propagates = True

_ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _alembic_config() -> Config:
    return Config(str(_ALEMBIC_INI))


def _run_upgrade() -> None:
    command.upgrade(_alembic_config(), "head")


def _run_downgrade() -> None:
    command.downgrade(_alembic_config(), "base")


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


@pytest_asyncio.fixture(scope="session")
async def db_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Real Postgres engine for model round-trip tests — requires a running
    Postgres reachable at DATABASE_URL (e.g. `docker compose up -d postgres`).

    Builds the schema by running the real Alembic migrations (in a worker
    thread — alembic/env.py does its own `asyncio.run`, which can't nest
    inside this fixture's already-running loop), not `Base.metadata.
    create_all()`. Some DB objects (expenses_sync_seq, the server_seq
    trigger) are hand-written raw SQL with no ORM-metadata equivalent —
    only running the actual migrations creates them, so this is the only
    way the sync tests see real trigger behavior, not just table shape.
    """
    await asyncio.to_thread(_run_upgrade)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    yield engine
    await engine.dispose()
    await asyncio.to_thread(_run_downgrade)


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """One test = one outer transaction, rolled back afterward — isolates
    tests from each other without recreating the schema every time.
    """
    async with db_engine.connect() as conn:
        outer_tx = await conn.begin()
        session = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield session
        finally:
            await session.close()
            await outer_tx.rollback()


@pytest_asyncio.fixture
async def user(db_session: AsyncSession) -> User:
    user = User()
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def device(db_session: AsyncSession, user: User) -> Device:
    device = Device(user_id=user.id, platform=Platform.IOS)
    db_session.add(device)
    await db_session.commit()
    return device
