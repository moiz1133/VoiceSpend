"""One ASGI middleware doing request-id correlation, structured access
logging, and the http_request_duration_seconds histogram — combined so a
request is only timed once and the log line and the metric observation
always agree on route/method/status/duration.

Log levels: 2xx/3xx/4xx -> info or warning (never error — a 429 or a
Phase 5 quota_exceeded 200 is an ordinary outcome, not a failure); 5xx ->
error, with a stack trace attached.
"""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import request_id_var
from app.core.metrics import HTTP_REQUEST_DURATION

logger = logging.getLogger("app.request")


def _route_label(request: Request) -> str:
    """The route *template* (e.g. "/api/v1/parse"), never the raw resolved
    path — an unmatched request (404, or a probing bot) would otherwise
    produce one label value per distinct path ever requested, which is
    exactly the unbounded-cardinality footgun this file's metric must avoid.
    """
    route = request.scope.get("route")
    return route.path if route is not None else "unmatched"


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        token = request_id_var.set(request_id)
        start = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            duration_s = time.perf_counter() - start
            route_label = _route_label(request)
            HTTP_REQUEST_DURATION.labels(
                route=route_label, method=request.method, status="500"
            ).observe(duration_s)
            logger.error(
                "request failed",
                exc_info=True,
                extra={
                    "route": route_label,
                    "method": request.method,
                    "status_code": 500,
                    "duration_ms": round(duration_s * 1000, 2),
                },
            )
            raise
        finally:
            request_id_var.reset(token)

        duration_s = time.perf_counter() - start
        route_label = _route_label(request)
        HTTP_REQUEST_DURATION.labels(
            route=route_label, method=request.method, status=str(response.status_code)
        ).observe(duration_s)

        if response.status_code >= 500:
            log = logger.error
        elif response.status_code >= 400:
            log = logger.warning
        else:
            log = logger.info
        log(
            "request completed",
            extra={
                "route": route_label,
                "method": request.method,
                "status_code": response.status_code,
                "duration_ms": round(duration_s * 1000, 2),
            },
        )
        response.headers["X-Request-Id"] = request_id
        return response
