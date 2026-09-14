"""FastAPI application factory."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.metrics import router as metrics_router
from app.api.v1.router import api_router
from app.cache.redis import close_redis_client, get_redis_client
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
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
    # Added last -> outermost, so request-id correlation, structured
    # access logging, and the http_request_duration_seconds histogram
    # cover every response (CORS preflights included), not just ones that
    # made it through routing.
    app.add_middleware(RequestContextMiddleware)

    app.include_router(api_router, prefix="/api/v1")
    # Not versioned/business API — plain infra, kept off /api/v1.
    app.include_router(metrics_router)

    return app


app = create_app()
