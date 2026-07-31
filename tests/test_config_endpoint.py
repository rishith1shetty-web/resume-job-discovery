"""Client configuration endpoint.

These assertions pin the rules CLAUDE.md marks non-negotiable, so a future
change that quietly weakens one fails here.
"""

from __future__ import annotations

from httpx import AsyncClient


async def test_config_returns_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/config")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["errors"] == []
    assert body["data"]["api_prefix"] == "/api/v1"


async def test_result_limit_options_are_20_50_100(client: AsyncClient) -> None:
    search = (await client.get("/api/v1/config")).json()["data"]["search"]

    assert search["result_limit_options"] == [20, 50, 100]


async def test_match_threshold_is_70_and_locked(client: AsyncClient) -> None:
    search = (await client.get("/api/v1/config")).json()["data"]["search"]

    # CLAUDE.md section 18: never lowered to fill quotas.
    assert search["min_match_score"] == 70
    assert search["min_match_score_locked"] is True


async def test_at_most_eight_portals(client: AsyncClient) -> None:
    search = (await client.get("/api/v1/config")).json()["data"]["search"]

    assert search["max_enabled_portals"] == 8


async def test_freshness_windows_are_ordered_newest_first(client: AsyncClient) -> None:
    search = (await client.get("/api/v1/config")).json()["data"]["search"]

    assert [window["value"] for window in search["freshness_windows"]] == [
        "last_24h",
        "last_3d",
        "last_7d",
        "last_14d",
        "last_30d",
        "older_active",
    ]


async def test_default_sort_is_best_overall(client: AsyncClient) -> None:
    search = (await client.get("/api/v1/config")).json()["data"]["search"]

    assert search["default_sort"] == "best_overall"
    assert "best_overall" in {option["value"] for option in search["sort_options"]}


async def test_application_statuses_contain_no_auto_apply_concepts(
    client: AsyncClient,
) -> None:
    application = (await client.get("/api/v1/config")).json()["data"]["application"]
    values = {status["value"] for status in application["statuses"]}

    assert values == {
        "not_applied",
        "saved",
        "applied",
        "interview",
        "rejected",
        "offer",
        "skipped",
    }
    assert application["default_status"] == "not_applied"

    # Expiry is a property of the job, not an application outcome; keeping it
    # out of this enum is what stops a stale listing overwriting APPLIED.
    assert "expired" not in values

    forbidden = {"easy_apply", "apply_priority", "unsupported_form", "auto_submit"}
    assert not (values & forbidden)


async def test_resume_formats_are_pdf_docx_txt(client: AsyncClient) -> None:
    resume = (await client.get("/api/v1/config")).json()["data"]["resume"]

    assert set(resume["supported_extensions"]) == {".pdf", ".docx", ".txt"}


async def test_phase_1_declares_no_authentication(client: AsyncClient) -> None:
    data = (await client.get("/api/v1/config")).json()["data"]

    assert data["auth_required"] is False
