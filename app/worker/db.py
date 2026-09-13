"""Per-task DB session helper for Celery workers.

Deliberately independent of app/db/session.py's module-level engine/
sessionmaker singletons — those are sized for the FastAPI process's single
long-lived event loop. Each Celery task instead runs its own asyncio.run()
call (a brand-new event loop every invocation), and asyncpg connections
are bound to the event loop that created them: reusing a cached engine
across separate asyncio.run() calls would eventually hand a task a
connection pool built on a dead loop. So every task creates a fresh
engine, uses it, and disposes it before returning — never the FastAPI
request-scoped `get_db` dependency.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings


@asynccontextmanager
async def worker_db_session() -> AsyncIterator[AsyncSession]:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        async with sessionmaker() as session:
            yield session
    finally:
        await engine.dispose()
