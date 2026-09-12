"""Aggregates all v1 routers."""

from fastapi import APIRouter

from app.api.v1 import health

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health")

# TODO(Phase 2+): include expenses, sync, parse, and billing routers here.
