"""CORS configuration for the Lovable React frontend."""

from __future__ import annotations

from httpx import AsyncClient

PREFLIGHT_HEADERS = {
    "Access-Control-Request-Method": "GET",
    "Access-Control-Request-Headers": "content-type",
}


async def test_allows_configured_local_origin(client: AsyncClient) -> None:
    response = await client.options(
        "/api/v1/health",
        headers={"Origin": "http://localhost:5173", **PREFLIGHT_HEADERS},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers["access-control-allow-credentials"] == "true"


async def test_allows_lovable_preview_subdomain_via_regex(client: AsyncClient) -> None:
    # Lovable preview URLs rotate, so they are matched by pattern.
    origin = "https://my-app-preview.lovable.app"
    response = await client.options(
        "/api/v1/health", headers={"Origin": origin, **PREFLIGHT_HEADERS}
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


async def test_rejects_unknown_origin(client: AsyncClient) -> None:
    response = await client.options(
        "/api/v1/health",
        headers={"Origin": "https://evil.example.com", **PREFLIGHT_HEADERS},
    )

    assert "access-control-allow-origin" not in response.headers


async def test_actual_request_carries_cors_headers(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/health", headers={"Origin": "http://localhost:5173"}
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


async def test_request_id_header_is_exposed_to_the_browser(
    client: AsyncClient,
) -> None:
    response = await client.get(
        "/api/v1/health", headers={"Origin": "http://localhost:5173"}
    )

    # Without this the frontend cannot read X-Request-ID from JS.
    assert "x-request-id" in response.headers["access-control-expose-headers"].lower()
