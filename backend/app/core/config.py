"""Application settings.

Every configurable value is read once from environment variables (or
``backend/.env``), validated, and exposed as a typed ``Settings`` object.
The application refuses to start if a value is missing, unsafe or malformed,
so misconfiguration is caught at startup instead of during a user request.

Usage:
    from app.core.config import get_settings

    settings = get_settings()
    settings.database_url.get_secret_value()
"""

from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

# backend/  (this file is backend/app/core/config.py)
BACKEND_DIR = Path(__file__).resolve().parents[2]

ASYNC_DRIVER = "postgresql+asyncpg"
MIN_JWT_SECRET_LENGTH = 32
PLACEHOLDER_MARKER = "CHANGE_ME"


class Settings(BaseSettings):
    """Typed, validated configuration. Field names map to UPPER_CASE env vars."""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # Never echo raw values (passwords, secrets) inside validation errors.
        hide_input_in_errors=True,
    )

    # ── Application ──
    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    sql_echo: bool = False

    # ── Database ──
    database_url: SecretStr
    test_database_url: SecretStr | None = None

    # ── Connection pool (per worker process) ──
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout: int = Field(default=30, ge=1, le=300, description="seconds")
    db_pool_recycle: int = Field(default=1800, ge=60, description="seconds")

    # ── Auth tokens ──
    jwt_secret_key: SecretStr
    jwt_algorithm: Literal["HS256"] = "HS256"
    access_token_expire_minutes: int = Field(default=15, ge=1, le=60)
    refresh_token_expire_days: int = Field(default=14, ge=1, le=90)
    refresh_reuse_grace_seconds: int = Field(default=30, ge=0, le=300)
    password_reset_token_expire_minutes: int = Field(default=30, ge=5, le=1440)

    # ── Links and email ──
    frontend_base_url: str = "http://localhost:3000"
    email_backend: Literal["console"] = "console"

    # ── Field validators ──

    @field_validator("database_url", "test_database_url")
    @classmethod
    def _require_async_postgres_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        if PLACEHOLDER_MARKER in raw:
            raise ValueError("still contains the CHANGE_ME placeholder")
        try:
            url = make_url(raw)
        except Exception as exc:  # malformed URL
            raise ValueError("is not a valid database URL") from exc
        if url.drivername != ASYNC_DRIVER:
            raise ValueError(f"must use the async driver '{ASYNC_DRIVER}://'")
        if not url.database:
            raise ValueError("must include a database name")
        return value

    @field_validator("jwt_secret_key")
    @classmethod
    def _require_strong_jwt_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if PLACEHOLDER_MARKER in raw:
            raise ValueError("still contains the CHANGE_ME placeholder")
        if len(raw) < MIN_JWT_SECRET_LENGTH:
            raise ValueError(
                f"must be at least {MIN_JWT_SECRET_LENGTH} characters "
                "(generate one with: openssl rand -hex 32)"
            )
        return value

    @field_validator("frontend_base_url")
    @classmethod
    def _normalise_frontend_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return value.rstrip("/")

    # ── Cross-field rules ──

    @model_validator(mode="after")
    def _check_environment_rules(self) -> "Settings":
        if self.test_database_url is not None:
            test_db = make_url(self.test_database_url.get_secret_value()).database or ""
            main_db = make_url(self.database_url.get_secret_value()).database or ""
            # Tests wipe data, so they must never point at the real database.
            if test_db == main_db:
                raise ValueError(
                    "TEST_DATABASE_URL must use a different database than DATABASE_URL"
                )
            if not test_db.endswith("_test"):
                raise ValueError(
                    "TEST_DATABASE_URL database name must end with '_test'"
                )

        if self.app_env == "production":
            if self.sql_echo:
                raise ValueError(
                    "SQL_ECHO must be false in production (it logs query data)"
                )
            if self.email_backend == "console":
                raise ValueError(
                    "EMAIL_BACKEND=console writes password-reset links to the logs; "
                    "configure a real email backend before running in production"
                )
            if not self.frontend_base_url.startswith("https://"):
                raise ValueError("FRONTEND_BASE_URL must use https:// in production")
        return self

    # ── Convenience properties ──

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def access_token_ttl(self) -> timedelta:
        return timedelta(minutes=self.access_token_expire_minutes)

    @property
    def refresh_token_ttl(self) -> timedelta:
        return timedelta(days=self.refresh_token_expire_days)

    @property
    def refresh_reuse_grace(self) -> timedelta:
        return timedelta(seconds=self.refresh_reuse_grace_seconds)

    @property
    def password_reset_token_ttl(self) -> timedelta:
        return timedelta(minutes=self.password_reset_token_expire_minutes)


@lru_cache
def get_settings() -> Settings:
    """Return the single, cached Settings instance.

    ``lru_cache`` means ``.env`` is read and validated once per process.
    Tests that change environment variables call ``get_settings.cache_clear()``.
    """
    return Settings()
