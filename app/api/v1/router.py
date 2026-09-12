"""Aggregates all v1 routers."""

from fastapi import APIRouter

from app.api.v1 import health, parse, sync

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health")
api_router.include_router(sync.router)
api_router.include_router(parse.router)

# TODO(Phase 5+): metering/quota, FX ingestion, and billing routers land here.
