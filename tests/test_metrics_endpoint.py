"""Tests for GET /metrics — app/api/metrics.py.

No DB/Redis needed: the endpoint only reads the in-process Prometheus
registry, so this stays in the fast unit-test bucket (no @pytest.mark.pg).
"""

from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.metrics import PARSE_REQUESTS
from app.main import create_app


async def test_metrics_open_when_no_token_configured(monkeypatch) -> None:
    monkeypatch.setenv("METRICS_TOKEN", "")
    get_settings.cache_clear()
    try:
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.get("/metrics")
        assert response.status_code == 200
        assert "# HELP" in response.text
        assert response.headers["content-type"].startswith("text/plain")
    finally:
        get_settings.cache_clear()


async def test_metrics_rejects_missing_auth_when_token_configured(monkeypatch) -> None:
    monkeypatch.setenv("METRICS_TOKEN", "s3cret")
    get_settings.cache_clear()
    try:
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.get("/metrics")
        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


async def test_metrics_rejects_wrong_token(monkeypatch) -> None:
    monkeypatch.setenv("METRICS_TOKEN", "s3cret")
    get_settings.cache_clear()
    try:
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.get("/metrics", headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


async def test_metrics_accepts_correct_bearer_token(monkeypatch) -> None:
    monkeypatch.setenv("METRICS_TOKEN", "s3cret")
    get_settings.cache_clear()
    try:
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.get("/metrics", headers={"Authorization": "Bearer s3cret"})
        assert response.status_code == 200
    finally:
        get_settings.cache_clear()


async def test_metrics_reflects_recorded_values() -> None:
    PARSE_REQUESTS.labels(provider="fake", model="fake-model", status="ok").inc()

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert 'parse_requests_total{model="fake-model",provider="fake",status="ok"}' in response.text
