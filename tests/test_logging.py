"""Tests for structured JSON logging and request-id correlation —
app/core/logging.py, app/core/middleware.py. No DB needed.
"""

import json
import logging

from httpx import ASGITransport, AsyncClient

from app.core.logging import JSONFormatter, redact_headers, request_id_var, trace_id_var
from app.main import create_app


def test_json_formatter_produces_valid_json_with_expected_keys() -> None:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname="x.py",
        lineno=1,
        msg="hello world",
        args=(),
        exc_info=None,
    )
    record.route = "/api/v1/parse"
    record.status_code = 200
    record.duration_ms = 12.34

    payload = json.loads(JSONFormatter().format(record))

    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["route"] == "/api/v1/parse"
    assert payload["status_code"] == 200
    assert payload["duration_ms"] == 12.34
    assert "timestamp" in payload


def test_json_formatter_omits_none_extras() -> None:
    record = logging.LogRecord(
        name="app.test", level=logging.INFO, pathname="x.py", lineno=1,
        msg="hi", args=(), exc_info=None,
    )
    record.request_id = None
    record.trace_id = None

    payload = json.loads(JSONFormatter().format(record))
    assert "request_id" not in payload
    assert "trace_id" not in payload


def test_json_formatter_includes_exception_info() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="app.test", level=logging.ERROR, pathname="x.py", lineno=1,
            msg="failed", args=(), exc_info=True,
        )
        import sys

        record.exc_info = sys.exc_info()

    payload = json.loads(JSONFormatter().format(record))
    assert "exc_info" in payload
    assert "ValueError: boom" in payload["exc_info"]


def test_redact_headers_hides_authorization_but_keeps_others() -> None:
    headers = {"Authorization": "Bearer secret-token", "Content-Type": "application/json"}
    redacted = redact_headers(headers)
    assert redacted["Authorization"] == "***REDACTED***"
    assert redacted["Content-Type"] == "application/json"


async def test_response_echoes_or_generates_request_id() -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/metrics")

    assert "X-Request-Id" in response.headers
    assert len(response.headers["X-Request-Id"]) > 0


async def test_response_honors_incoming_request_id() -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/metrics", headers={"X-Request-Id": "test-request-id-123"})

    assert response.headers["X-Request-Id"] == "test-request-id-123"


def test_context_vars_default_to_none() -> None:
    # Sanity check the contextvars exist with a safe default — actual
    # per-request binding is exercised end-to-end by the middleware tests
    # above (the header round-trip) and by app/api/v1/parse.py's trace_id
    # binding, which isn't independently observable without a real
    # Langfuse trace id.
    assert request_id_var.get() is None
    assert trace_id_var.get() is None
