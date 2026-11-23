"""Logging configuration.

Gives every log line, from our code, Uvicorn and SQLAlchemy alike:

* one consistent format: readable text in development, one JSON object per line
  in production (easy for log tools to search);
* the current ``request_id``, so every line written while handling one HTTP
  request can be found together, and matched to the ``request_id`` returned in
  API error responses (docs/api.md, section 3);
* redaction of secrets (passwords, tokens, JWTs, password hashes, database
  passwords) before anything is written.

Usage:
    import logging

    logger = logging.getLogger(__name__)
    logger.info("Task %s created", task_key)

``configure_logging()`` is called once at application startup (app/main.py).
The request-id middleware (also app/main.py) calls ``set_request_id()``.
"""

import json
import logging
import re
import secrets
import sys
import time
from contextvars import ContextVar, Token
from datetime import UTC, datetime

from app.core.config import Settings, get_settings

# ── Request id ────────────────────────────────────────────────────────────────
# A ContextVar holds a separate value for each request, even when many requests
# run concurrently on one event loop. Tasks started with asyncio.create_task()
# or asyncio.gather() inside a request copy it automatically.

NO_REQUEST_ID = "-"
request_id_var: ContextVar[str] = ContextVar("request_id", default=NO_REQUEST_ID)


def new_request_id() -> str:
    """Return a random 16-character hex id, e.g. ``0f6c2a1e9b7d4c35``."""
    return secrets.token_hex(8)


def get_request_id() -> str:
    """Return the current request's id, or ``-`` outside a request."""
    return request_id_var.get()


def set_request_id(request_id: str) -> Token[str]:
    """Set the id for the current request. Keep the token to reset it afterwards."""
    return request_id_var.set(request_id)


def reset_request_id(token: Token[str]) -> None:
    """Restore the id that was active before ``set_request_id``."""
    request_id_var.reset(token)


class RequestIdFilter(logging.Filter):
    """Attach the current request id to every record as ``record.request_id``."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


# ── Secret redaction ──────────────────────────────────────────────────────────

REDACTED = "[REDACTED]"

# Field names whose values must never appear in logs.
_SECRET_KEYS = (
    "password",
    "new_password",
    "password_hash",
    "token",
    "token_hash",
    "access_token",
    "refresh_token",
    "secret",
    "jwt_secret_key",
)

_REDACTION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Argon2 password hashes. First, because they contain commas, "=" and "$".
    (
        re.compile(
            r"\$argon2(?:id|i|d)\$v=\d+\$m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+"
        ),
        REDACTED,
    ),
    # Any JWT (three base64url parts, first starts with eyJ)
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), REDACTED),
    # Authorization: Bearer <token>  (8+ characters, so the plain word "bearer" is kept)
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9\-._~+/]{8,}=*"), rf"\1{REDACTED}"),
    # password=..., "refresh_token": "...", token: ...  (query strings, JSON, reprs)
    (
        re.compile(
            r"""(?i)(["']?\b(?:"""
            + "|".join(_SECRET_KEYS)
            + r""")["']?\s*[:=]\s*["']?)(?!\[REDACTED\])([^"'\s,&}\]]+)"""
        ),
        rf"\1{REDACTED}",
    ),
    # scheme://user:PASSWORD@host  (database and other connection URLs)
    (
        re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^:/\s@]+:)([^@\s]+)(@)", re.I),
        rf"\1{REDACTED}\3",
    ),
)


def redact(text: str) -> str:
    """Replace secret values in ``text`` with ``[REDACTED]``."""
    for pattern, replacement in _REDACTION_RULES:
        text = pattern.sub(replacement, text)
    return text


# Records logged with extra={"unredacted": True} skip redaction. Only the
# development console email sender uses this, so the reset link it prints is
# usable. config.py refuses EMAIL_BACKEND=console in production.
UNREDACTED_ATTR = "unredacted"


# ── Formatters ────────────────────────────────────────────────────────────────


class RedactingFormatter(logging.Formatter):
    """Text formatter that redacts the whole output, including tracebacks."""

    converter = time.gmtime  # timestamps in UTC, like the database

    def format(self, record: logging.LogRecord) -> str:
        output = super().format(record)
        if getattr(record, UNREDACTED_ATTR, False):
            return output
        return redact(output)


class JsonFormatter(RedactingFormatter):
    """One JSON object per line, for production log collection."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, str] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", NO_REQUEST_ID),
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        output = json.dumps(entry, ensure_ascii=False)
        if getattr(record, UNREDACTED_ATTR, False):
            return output
        return redact(output)


TEXT_FORMAT = "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s"


# ── Setup ─────────────────────────────────────────────────────────────────────

# Third-party loggers that should use our handler and format.
_ROUTED_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "sqlalchemy",
    "alembic",
)


def configure_logging(settings: Settings | None = None) -> None:
    """Configure the root logger once. Safe to call again (replaces handlers)."""
    settings = settings or get_settings()

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    if settings.is_production:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(RedactingFormatter(TEXT_FORMAT))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Uvicorn and Alembic install their own handlers; remove them so every line
    # goes through ours exactly once (no duplicates, same format, redacted).
    for name in _ROUTED_LOGGERS:
        third_party = logging.getLogger(name)
        third_party.handlers.clear()
        third_party.propagate = True

    # SQL statements: logged only when SQL_ECHO=true. The engine (db/session.py)
    # does NOT pass echo=True, because that adds a second handler of its own.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.sql_echo else logging.WARNING
    )


"""logging.py sets up logging for the whole backend. Every log line, whether it comes from your
code, Uvicorn or SQLAlchemy, gets three things:

A request ID. Each HTTP request gets a random ID like [aaaa1111bbbb2222], which is printed on every
line logged while handling that request. A ContextVar keeps the IDs separate when many requests run
at once. The same ID appears in API error responses, so you can match a user's error to its log
lines.

Secret hiding. Passwords, tokens, JWTs, Argon2 hashes and database passwords are replaced with
[REDACTED] before anything is written, so secrets can't leak into logs by accident. The only
exception is the development email sender, which needs to print a working reset link.

One format. On your laptop, lines are readable text:


2026-11-17 07:15:05 INFO [aaaa1111bbbb2222] app.auth: login attempt password=[REDACTED]
In production, each line is one JSON object so log tools can search it. Times are always in UTC.

configure_logging() is called once at startup. It sends everything to the terminal (stdout) with
that format, applies LOG_LEVEL from .env, takes over Uvicorn's, SQLAlchemy's and Alembic's own log
output so there are no duplicate or unredacted lines, and shows SQL queries only when SQL_ECHO=true.

Other files just write logger = logging.getLogger(__name__) and log normally. The request ID, secret
hiding and formatting happen automatica


logging.py sets up logging for the whole backend. Every log line, whether it comes from your code,
Uvicorn or SQLAlchemy, gets three things:

A request ID. Each HTTP request gets a random ID like [aaaa1111bbbb2222], which is printed on every
line logged while handling that request. A ContextVar keeps the IDs separate when many requests run
at once. The same ID appears in API error responses, so you can match a user's error to its log
lines.

Secret hiding. Passwords, tokens, JWTs, Argon2 hashes and database passwords are replaced with
[REDACTED] before anything is written, so secrets can't leak into logs by accident. The only
exception is the development email sender, which needs to print a working reset link.

One format. On your laptop, lines are readable text:


2026-11-17 07:15:05 INFO [aaaa1111bbbb2222] app.auth: login attempt password=[REDACTED]
In production, each line is one JSON object so log tools can search it. Times are always in UTC.

configure_logging() is called once at startup. It sends everything to the terminal (stdout) with
that format, applies LOG_LEVEL from .env, takes over Uvicorn's, SQLAlchemy's and Alembic's own log
output so there are no duplicate or unredacted lines, and shows SQL queries only when SQL_ECHO=true.

Other files just write logger = logging.getLogger(__name__) and log normally. The request ID, secret
hiding and formatting happen automatically.



"""
