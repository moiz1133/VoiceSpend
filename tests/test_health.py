"""Tests for /health/live and /health/ready."""

from httpx import AsyncClient


async def test_liveness(healthy_client: AsyncClient) -> None:
    response = await healthy_client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_ok(healthy_client: AsyncClient) -> None:
    response = await healthy_client.get("/api/v1/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"] == "ok"


async def test_readiness_fails_when_dependencies_down(unhealthy_client: AsyncClient) -> None:
    response = await unhealthy_client.get("/api/v1/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert "error" in body["checks"]["postgres"]
    assert "error" in body["checks"]["redis"]
