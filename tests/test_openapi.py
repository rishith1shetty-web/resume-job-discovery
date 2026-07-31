"""OpenAPI documentation.

The schema is the contract the Lovable frontend generates its client from, so
its shape is asserted rather than assumed.
"""

from __future__ import annotations

from httpx import AsyncClient


async def test_openapi_schema_is_served(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "JobFinder"
    assert schema["openapi"].startswith("3.")


async def test_documented_paths_are_versioned(client: AsyncClient) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]

    assert "/api/v1/health" in paths
    assert "/api/v1/config" in paths
    # Every functional route lives under the version prefix.
    assert all(path.startswith("/api/v1") for path in paths)


async def test_responses_are_documented_as_envelopes(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    health_200 = schema["paths"]["/api/v1/health"]["get"]["responses"]["200"]
    ref = health_200["content"]["application/json"]["schema"]["$ref"]
    component = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]

    # The frontend relies on this exact envelope for every endpoint.
    assert set(component["properties"]) == {"success", "data", "message", "errors"}


async def test_swagger_ui_is_available(client: AsyncClient) -> None:
    response = await client.get("/docs")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


async def test_redoc_is_available(client: AsyncClient) -> None:
    assert (await client.get("/redoc")).status_code == 200


async def test_no_auto_apply_concepts_are_exposed(client: AsyncClient) -> None:
    # A structural guard against auto-apply features re-entering the API.
    body = (await client.get("/openapi.json")).text.lower()

    for forbidden in ("easyapply", "easy_apply", "autosubmit", "auto_submit"):
        assert forbidden not in body
