"""GET /metrics — the Prometheus scrape endpoint.

Deliberately NOT under /api/v1 (it's infra, not a business/versioned API)
and NOT public: guarded by a static bearer token (settings.METRICS_TOKEN)
when one is configured. Leaving METRICS_TOKEN blank leaves the endpoint
open — acceptable only when this service is reachable exclusively from a
private network / internal scrape target, never when exposed publicly;
see the warning in .env.example.
"""

import hmac
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Response

from app.core.config import get_settings
from app.core.metrics import PROMETHEUS_CONTENT_TYPE, render_prometheus_metrics

router = APIRouter()


@router.get("/metrics")
async def metrics(authorization: Annotated[str | None, Header()] = None) -> Response:
    settings = get_settings()
    if settings.METRICS_TOKEN:
        expected = f"Bearer {settings.METRICS_TOKEN}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    return Response(content=render_prometheus_metrics(), media_type=PROMETHEUS_CONTENT_TYPE)
