"""Central exception handling.

Every failure path must produce the same envelope shape as a success, so the
React client parses one structure regardless of outcome.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field

from app.core.exceptions import ErrorDetail, NotFoundError, UnprocessableError


class _LimitPayload(BaseModel):
    """Module-level on purpose.

    This module uses postponed annotations, so a model defined inside a test
    function cannot be resolved by FastAPI and would silently be treated as a
    query parameter instead of a request body.
    """

    requested_limit: int = Field(ge=20)


def _assert_error_envelope(body: dict) -> None:
    assert body["success"] is False
    assert body["data"] is None
    assert isinstance(body["errors"], list) and body["errors"]
    for error in body["errors"]:
        assert set(error) == {"code", "field", "message"}


async def test_unknown_route_returns_error_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    _assert_error_envelope(response.json())


async def test_app_error_is_rendered_as_envelope(app: FastAPI) -> None:
    @app.get("/api/v1/_test/missing")
    async def _missing() -> None:
        raise NotFoundError(message="Resume not found.")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/v1/_test/missing")

    assert response.status_code == 404
    body = response.json()
    _assert_error_envelope(body)
    assert body["message"] == "Resume not found."
    assert body["errors"][0]["code"] == "not_found"


async def test_business_rule_error_carries_field_path(app: FastAPI) -> None:
    @app.get("/api/v1/_test/too-many-portals")
    async def _too_many() -> None:
        raise UnprocessableError(
            message="At most 8 portals may be enabled.",
            details=[
                ErrorDetail(
                    code="max_enabled_portals_exceeded",
                    field="body.selected_portal_ids",
                    message="Select 8 portals or fewer.",
                )
            ],
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/v1/_test/too-many-portals")

    assert response.status_code == 422
    error = response.json()["errors"][0]
    assert error["field"] == "body.selected_portal_ids"
    assert error["code"] == "max_enabled_portals_exceeded"


async def test_validation_error_uses_dotted_field_paths(app: FastAPI) -> None:
    @app.post("/api/v1/_test/validate")
    async def _validate(payload: _LimitPayload) -> dict[str, int]:
        return {"requested_limit": payload.requested_limit}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/v1/_test/validate", json={"requested_limit": 1})

    assert response.status_code == 422
    body = response.json()
    _assert_error_envelope(body)
    assert body["message"] == "Validation failed."
    # Dotted path lets the frontend bind the message to the right input.
    assert body["errors"][0]["field"] == "body.requested_limit"


async def test_unexpected_exception_does_not_leak_internals(app: FastAPI) -> None:
    @app.get("/api/v1/_test/boom")
    async def _boom() -> None:
        raise RuntimeError("connection string postgresql://user:secret@host/db")

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        with pytest.raises(RuntimeError):
            # ASGITransport re-raises so the failure is visible in tests; the
            # assertion that matters is that nothing above swallowed it into a
            # 200. Real deployments return the generic 500 envelope below.
            await client.get("/api/v1/_test/boom")


async def test_unexpected_exception_returns_generic_500(app: FastAPI) -> None:
    @app.get("/api/v1/_test/boom2")
    async def _boom2() -> None:
        raise RuntimeError("postgresql://user:secret@host/db")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/v1/_test/boom2")

    assert response.status_code == 500
    body = response.json()
    _assert_error_envelope(body)
    # No secrets, no stack trace, no driver detail in the client-facing body.
    assert "secret" not in response.text
    assert body["errors"][0]["code"] == "internal_error"
