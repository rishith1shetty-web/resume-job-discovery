"""Application configuration, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.constants import Environment, LogFormat

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _split_csv(value: Any) -> Any:
    """Accept both a comma-separated string and a real list for list fields."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -------------------------------------------------------
    app_name: str = "JobFinder"
    app_version: str = "0.1.0"
    api_v1_prefix: str = "/api/v1"
    environment: Environment = Environment.LOCAL
    debug: bool = False

    # 8000 is deliberately avoided; it belongs to the separate auto-apply project.
    api_host: str = "127.0.0.1"
    api_port: int = 8100

    # --- Database ----------------------------------------------------------
    # Dedicated database on the existing local PostgreSQL 16 server. The
    # separate `linkedin_auto_apply` database belongs to another project and
    # is never read, written, migrated or inspected from here.
    postgres_user: str = "postgres"
    postgres_password: str = "change-me"
    postgres_db: str = "resume_job_discovery"
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    database_url: str | None = None

    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_echo: bool = False

    # --- Redis -------------------------------------------------------------
    # Reserved for Phase 7 background workers. Nothing connects to it yet.
    redis_url: str = "redis://127.0.0.1:6380/0"

    # --- CORS --------------------------------------------------------------
    # NoDecode: pydantic-settings would otherwise try to JSON-decode this env
    # var before the validator below gets a chance to split the CSV form.
    cors_allow_origins: Annotated[list[str], NoDecode, Field(default_factory=list)]
    cors_allow_origin_regex: str | None = None
    cors_allow_credentials: bool = True

    # --- Logging -----------------------------------------------------------
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.CONSOLE

    # --- Storage -----------------------------------------------------------
    storage_dir: Path = PROJECT_ROOT / "storage"
    max_upload_mb: int = 10

    # --- Single-user mode --------------------------------------------------
    # This runs as a local single-user application for now. The database keeps
    # its user_id-based architecture unchanged, so authentication and real
    # multi-user support can be added later without a data-model redesign.
    single_user_mode: bool = True
    default_user_email: str = "local@jobfinder.local"
    default_user_name: str | None = "Local User"

    # --- Resume analysis ---------------------------------------------------
    # Optional LLM assistance is defined behind an interface but is NOT
    # implemented. Deterministic extraction is authoritative and must work
    # with no API key. Enabling this raises at startup until approved.
    enable_llm_assist: bool = False

    # --- Locked domain rules ----------------------------------------------
    # CLAUDE.md section 18: the threshold is locked and must never be lowered
    # to fill a quota. It is configurable only so it can be raised.
    min_match_score: int = Field(default=70, ge=70, le=100)
    max_enabled_portals: int = Field(default=8, ge=1, le=8)

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("cors_allow_origin_regex", mode="before")
    @classmethod
    def _blank_regex_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: Any) -> Any:
        if isinstance(value, str):
            level = value.strip().upper()
            allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
            if level not in allowed:
                raise ValueError(f"log_level must be one of {sorted(allowed)}")
            return level
        return value

    @model_validator(mode="after")
    def _reject_unapproved_llm_assist(self) -> Settings:
        if self.enable_llm_assist:
            raise ValueError(
                "enable_llm_assist is not implemented and must remain false. "
                "Deterministic extraction is authoritative; LLM assistance "
                "requires explicit approval before being built."
            )
        return self

    @model_validator(mode="after")
    def _validate_cors(self) -> Settings:
        # Starlette silently ignores credentials when origins is ["*"], which
        # produces a confusing browser-side failure. Fail loudly at startup.
        if self.cors_allow_credentials and "*" in self.cors_allow_origins:
            raise ValueError(
                "cors_allow_credentials=true is incompatible with a wildcard "
                "origin. List explicit origins or use cors_allow_origin_regex."
            )
        return self

    @property
    def sqlalchemy_url(self) -> str:
        """Async SQLAlchemy URL. DATABASE_URL wins when explicitly provided."""
        if self.database_url:
            return self.database_url
        return str(
            PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=self.postgres_user,
                password=self.postgres_password,
                host=self.postgres_host,
                port=self.postgres_port,
                path=self.postgres_db,
            )
        )

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton. Call ``get_settings.cache_clear()`` in tests."""
    return Settings()
