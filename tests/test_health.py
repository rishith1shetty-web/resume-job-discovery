"""Health endpoint behaviour."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


async def test_health_returns_ok_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()

    # Envelope contract
    assert body["success"] is True
    assert body["errors"] == []
    assert body["message"] is None

    data = body["data"]
    assert data["status"] == "ok"
    assert data["api_version"] == "v1"
    assert data["uptime_seconds"] >= 0


async def test_health_reports_each_dependency(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/health")).json()
    checks = {check["name"]: check for check in body["data"]["checks"]}

    assert checks["database"]["status"] == "up"
    # Redis is provisioned but unused in Phase 1; it must not claim to be up.
    assert checks["redis"]["status"] == "not_configured"


async def test_health_returns_503_when_database_is_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _ping_down() -> bool:
        return False

    monkeypatch.setattr("app.db.session.ping_database", _ping_down)

    response = await client.get("/api/v1/health")

    # Status code alone must be enough for an uptime monitor.
    assert response.status_code == 503
    body = response.json()
    # `success` mirrors the HTTP status, as it does for every other endpoint.
    assert body["success"] is False
    assert body["message"] == "Service degraded."
    # Health is the documented exception to "data is null on error": the
    # payload is what tells the operator which dependency failed.
    assert body["data"]["status"] == "degraded"

    database = next(c for c in body["data"]["checks"] if c["name"] == "database")
    assert database["status"] == "down"


async def test_health_echoes_request_id(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/health", headers={"X-Request-ID": "test-correlation-id"}
    )

    assert response.headers["X-Request-ID"] == "test-correlation-id"
