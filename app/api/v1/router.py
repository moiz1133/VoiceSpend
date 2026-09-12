"""Aggregates all v1 routers."""

from fastapi import APIRouter

from app.api.v1 import health, sync

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health")
api_router.include_router(sync.router)

# TODO(Phase 4+): parse (LLM), FX, and billing routers land here.
