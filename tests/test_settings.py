"""Configuration loading and guardrails."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.constants import MAX_ENABLED_PORTALS, Environment


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "postgres_user": "postgres",
        "postgres_password": "secret",
        "postgres_db": "resume_job_discovery",
        "postgres_host": "127.0.0.1",
        "postgres_port": 5432,
        "cors_allow_origins": [],
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


def test_sqlalchemy_url_uses_the_async_driver() -> None:
    url = _settings().sqlalchemy_url

    assert url.startswith("postgresql+asyncpg://")
    assert url.endswith("/resume_job_discovery")


def test_explicit_database_url_wins() -> None:
    override = "postgresql+asyncpg://u:p@db:5432/other"

    assert _settings(database_url=override).sqlalchemy_url == override


def test_project_never_defaults_to_the_other_projects_database() -> None:
    # Guardrail: the LinkedIn/Dice project owns `linkedin_auto_apply`.
    assert "linkedin_auto_apply" not in _settings().sqlalchemy_url
    assert Settings.model_fields["postgres_db"].default == "resume_job_discovery"


def test_default_api_port_avoids_the_other_projects_port() -> None:
    # 8000 is occupied by the separate auto-apply project.
    assert Settings.model_fields["api_port"].default == 8100


def test_cors_origins_parse_from_a_comma_separated_string() -> None:
    settings = _settings(cors_allow_origins="http://a.test, http://b.test")

    assert settings.cors_allow_origins == ["http://a.test", "http://b.test"]


def test_blank_cors_regex_becomes_none() -> None:
    assert _settings(cors_allow_origin_regex="   ").cors_allow_origin_regex is None


def test_wildcard_origin_with_credentials_is_rejected() -> None:
    # Starlette would silently drop credentials; fail loudly at startup instead.
    with pytest.raises(ValidationError, match="wildcard origin"):
        _settings(cors_allow_origins=["*"], cors_allow_credentials=True)


def test_match_threshold_cannot_be_lowered_below_70() -> None:
    # CLAUDE.md section 18: the threshold is locked. It may be raised, never
    # lowered to fill a quota.
    with pytest.raises(ValidationError):
        _settings(min_match_score=60)

    assert _settings(min_match_score=85).min_match_score == 85


def test_enabled_portal_cap_cannot_exceed_the_ceiling() -> None:
    # The ceiling is one constant (app.core.constants.MAX_ENABLED_PORTALS) so
    # the settings bound and the request schema cannot drift apart. Asserted
    # against the constant rather than a literal: the point is that the bound
    # *is* the ceiling, not that the ceiling is any particular number.
    with pytest.raises(ValidationError):
        _settings(max_enabled_portals=MAX_ENABLED_PORTALS + 1)

    assert (
        _settings(max_enabled_portals=MAX_ENABLED_PORTALS).max_enabled_portals
        == MAX_ENABLED_PORTALS
    )


def test_the_portal_ceiling_is_sixteen() -> None:
    # Raised from 8 so further portals can be added without retiring a working
    # one. A ceiling, not a target: it enables no portal by itself.
    assert MAX_ENABLED_PORTALS == 16


def test_eight_portals_is_still_accepted() -> None:
    # The previous ceiling must keep working: raising a maximum must not turn
    # a formerly valid configuration into an invalid one.
    assert _settings(max_enabled_portals=8).max_enabled_portals == 8


def test_a_smaller_cap_is_still_allowed() -> None:
    # An operator may still pin the cap below the ceiling; raising the ceiling
    # does not force anyone up to it.
    assert _settings(max_enabled_portals=3).max_enabled_portals == 3


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError, match="log_level"):
        _settings(log_level="chatty")


def test_log_level_is_case_insensitive() -> None:
    assert _settings(log_level="debug").log_level == "DEBUG"


def test_is_production_flag() -> None:
    assert _settings(environment=Environment.PRODUCTION).is_production is True
    assert _settings(environment=Environment.LOCAL).is_production is False
