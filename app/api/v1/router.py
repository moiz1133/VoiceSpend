"""Aggregates all v1 routers."""

from fastapi import APIRouter

from app.api.v1 import health, parse, sync, webhooks

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health")
api_router.include_router(sync.router)
api_router.include_router(parse.router)
api_router.include_router(webhooks.router)

# TODO(Phase 6+): FX ingestion and further billing providers land here.
