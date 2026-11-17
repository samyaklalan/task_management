"""Application settings.

Every configurable value is read once from environment variables (or
``backend/.env``), validated, and exposed as a typed ``Settings`` object.
The application refuses to start if a value is missing, unsafe or malformed,
so misconfiguration is caught at startup instead of during a user request.

Usage:
    from app.core.config import get_settings

    settings = get_settings()
    settings.database_url.get_secret_value()

    In short, SecretStr keeps the value hidden and get_secret_value() gives you the real
    value only where you need it SecretStr hides the value so it doesn't leak by accident:


settings = get_settings()

print(settings.jwt_secret_key)          # SecretStr('**********')
repr(settings)                          # secrets show as '**********'
settings.jwt_secret_key.get_secret_value()  # 'abc123...' (the real string)
When a settings object gets printed, logged or included in an error message, you see **********
instead of your DB password or JWT key. The only way to get the real value is to call
get_secret_value() explicitly, so every place that reads a secret is deliberate and easy to grep
for.
It isn't a Python built-in. get_secret_value() is a method from the Pydantic library. It exists only
on Pydantic's SecretStr (and SecretBytes) types, and you import those from pydantic:


from pydantic import SecretStr
What it does
It returns the real hidden value stored inside a SecretStr.

If you declare a field as SecretStr, Pydantic wraps the value so it's hidden by default:
SecretStr masks values when printed: repr(settings.database_url) shows SecretStr('**********'). The
real value is only available through an explicit .get_secret_value() call, which stands out in code
review.
hide_input_in_errors=True keeps raw values out of validation error messages too. Without it, a bad
DATABASE_URL would print the full URL, password included, into the logs.
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
                raise ValueError("TEST_DATABASE_URL database name must end with '_test'")

        if self.app_env == "production":
            if self.sql_echo:
                raise ValueError("SQL_ECHO must be false in production (it logs query data)")
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


"""Why use it
It reads like data. access_token_ttl is a value (how long a token lives), not an action, so writing
settings.access_token_ttl feels natural.
The value is computed from other fields every time, so it never goes stale. If
access_token_expire_minutes = 15, then access_token_ttl is always timedelta(minutes=15).
 There's no second copy to keep in sync.
Property	Built from	Returns
is_production	app_env	True/False
access_token_ttl	access_token_expire_minutes	timedelta
refresh_token_ttl	refresh_token_expire_days	timedelta
refresh_reuse_grace	refresh_reuse_grace_seconds	timedelta
password_reset_token_ttl	password_reset_token_expire_minutes	timedelta"""
"""
whats the use of proprty in production projects

In production Python projects, @property protects data integrity, keeps public APIs stable during
refactoring, and prevents stale or duplicated state. Engineering teams rely on it so they can start
with simple classes and add strict business rules later without breaking external callers or
downstream services.

1. Backward-Compatible Refactoring

In large codebases, changing a plain attribute into a method (user.full_name to
user.get_full_name()) forces you to update hundreds of files.
• Zero Breaking Changes. You can replace a stored variable with a @property method while keeping the
  exact same dot-notation syntax (user.full_name). External modules, templates, and API serializers
  continue working without edits.
• Smooth Deprecations. You can wrap an old attribute name in a @property that logs a deprecation
  warning and redirects to a new field. This gives other teams time to migrate safely.

2. Enforcing Business Rules & Data Validation

Production systems must reject bad data before it corrupts a database or crashes a background
worker.
• Guardrails on Mutation. Using @<name>.setter, you can enforce rules like ensuring an order's
  discount is between 0 and 100, or stripping whitespace from an email address automatically.
• Single Source of Truth. Instead of scattering if price < 0: checks across ten different views or
  services, the validation lives inside the model itself.

3. Preventing Stale Data (Derived State)

Storing both raw inputs and calculated results in an object often leads to bugs when one updates
without the other.
• Always-Fresh Values. Instead of storing order.total_price as a static variable that might go out
  of sync, you make total_price a @property that sums self.items and self.tax dynamically.
• Read-Only Guarantees. Omitting the setter ensures no developer can accidentally overwrite
  order.total_price = 50 directly in business logic.

4. Lazy Loading Expensive Resources

In web frameworks (like Django or FastAPI) and data pipelines, initializing heavy connections
upfront slows down startup and wastes memory.
• On-Demand Initialization. A @property can check if a database client, ML model, or S3 connection
  exists (if self._client is None:), create it only on first access, and cache it for future calls.
• Clean Integration with Frameworks. Libraries like Django, SQLAlchemy, and Pydantic inspect
  @property attributes naturally to serialize computed fields into JSON responses or admin panels.

Production Rules of Thumb

To avoid hidden bugs in live systems, senior engineers follow strict boundaries with @property:
• Keep It Fast. Never put network requests, heavy database queries, or slow loops inside a standard
  @property without caching. Callers expect attribute access (obj.x) to be instant.
• Avoid Side Effects. Reading a @property should never modify database records, send emails, or
  mutate other objects. Use a clear action method like order.calculate_and_save() instead.
Are you working with a specific framework like Django, FastAPI, or Pydantic where you want to see
how @property fits into models and schemas?

in real life if a settings is there and function so 50 files changes means 50 objects will becalling
that function using proprty is like calling direct;y

Yes, you nailed it. In real-world production projects, using @property allows all 50 files to keep
reading settings.API_URL directly like a normal variable, even after you change it behind the scenes
into a function that fetches or calculates the value dynamically.

How This Saves You in Real Life

Imagine you start a project with a simple Settings class where API_URL is just a plain variable.
Over time, 50 different files in your project read settings.API_URL.
Later, your requirements change: instead of a static URL, API_URL must now check an environment
variable or switch based on whether the app is in dev or prod mode.
• Without @property (The Painful Way). You convert API_URL into a method get_api_url(). Now you have
  to hunt down all 50 files and change settings.API_URL to settings.get_
api_url(), risking typos, merge conflicts, and broken code.
• With @property (The Clean Way). You convert API_URL into a @property method inside the Settings
  class. All 50 files keep calling settings.API_URL without changing a single character.
"""

"""
Yes, you understand the core idea completely—with one small clarification about creating objects.
Whether you use a regular method or @property, you still use the same settings object in those 50
files. The real difference is how you access it (settings.API_URL vs settings.API_URL()), which lets
you change the logic in 1 root file instead of editing 50 files.

Breaking Down Your Two Points

• 1. Editing Only the Root File (You are 100% right). 
When API_URL is defined as a @property in your 
root settings.py file, all 50 files treat it like a
 plain attribute (settings.API_URL). When you update 
 the logic inside settings.py, every single one of those 50 files gets the new behavior
 automatically without any code changes.
• 2. Creating Objects (How it actually works).
 You do not need to create a new Settings() object inside each of the 50 files, regardless of
 whether you use @property or a method. Usually, you create one shared instance (settings =
 Settings()) in your root file and import that single settings object into all 50 files.
"""


@lru_cache
def get_settings() -> Settings:
    """Return the single, cached Settings instance.

        ``lru_cache`` means ``.env`` is read and validated once per process.
        Tests that change environment variables call ``get_settings.cache_clear()``.
       @lru_cache on get_settings(). .env is read and validated once per process, and every module
       shares that one object. FastAPI can also inject it with Depends(get_settings), and tests can
       swap it out.

    The timedelta properties (access_token_ttl etc.) let security.py write now +
    settings.access_token_ttl instead of repeating “minutes or days?” arithmetic everywhere.
    """
    return Settings()
