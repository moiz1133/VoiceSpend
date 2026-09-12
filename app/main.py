"""FastAPI application factory."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.cache.redis import close_redis_client, get_redis_client
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import dispose_engine, get_engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    settings = get_settings()

    # Warm up the DB engine and Redis pool, and fail fast if unreachable.
    engine = get_engine()
    async with engine.connect():
        logger.info("Database connection established (env=%s)", settings.ENV)

    redis_client = get_redis_client()
    await redis_client.ping()
    logger.info("Redis connection established")

    yield

    await dispose_engine()
    await close_redis_client()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Voicespend API",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix="/api/v1")

    return app


app = create_app()
