"""Database engine, connection pool and per-request sessions.

* One ``AsyncEngine`` per process. It owns the connection pool, sized from
  settings (docs/database.md is the schema; .env holds the pool numbers).
* ``get_session()`` is a FastAPI dependency: every request gets its own
  ``AsyncSession``, which is rolled back on error and always closed, so its
  connection always goes back to the pool.

Transaction rule used across the project:
    repositories run queries but never commit;
    services decide when a unit of work is complete and call ``session.commit()``.
A request that only reads never commits; closing the session ends its transaction.
"""

import logging
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# PostgreSQL limits applied to every connection this app opens (asyncpg sends
# them when the connection starts). They stop one slow query, one stuck lock or
# one forgotten transaction from holding a pooled connection forever.
SERVER_SETTINGS: dict[str, str] = {
    "application_name": "task_management_api",  # visible in pg_stat_activity
    "timezone": "UTC",
    "statement_timeout": "30s",  # cancel any single query running longer
    "lock_timeout": "10s",  # give up waiting for a row lock (e.g. FOR UPDATE)
    "idle_in_transaction_session_timeout": "60s",  # end transactions left open
}


def create_engine(
    settings: Settings | None = None, *, url: str | None = None
) -> AsyncEngine:
    """Build an engine. ``url`` overrides the configured one (tests use the test DB)."""
    settings = settings or get_settings()
    return create_async_engine(
        url or settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        # Check a connection is alive before handing it out, so a database
        # restart doesn't surface as errors on the next requests.
        pool_pre_ping=True,
        # In production, SQLAlchemy's errors and logs omit the bound parameter
        # values (emails, names, token hashes). PostgreSQL's own error text can
        # still quote a value, e.g. "Key (email)=(...) already exists"; that only
        # reaches server logs, never API responses (app/core/exceptions.py).
        hide_parameters=settings.is_production,
        # SQL logging is controlled by SQL_ECHO in app/core/logging.py; echo=True
        # here would add a second, unredacted handler.
        echo=False,
        connect_args={"server_settings": SERVER_SETTINGS},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: after commit, objects keep their loaded values.
    # Otherwise the next attribute access would trigger a hidden query, which
    # AsyncSession cannot run implicitly.
    return async_sessionmaker(engine, expire_on_commit=False)


# ── Process-wide engine (created on first use, disposed at shutdown) ──────────

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine()
        _session_factory = create_session_factory(_engine)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    if _session_factory is None:  # always set together with _engine
        raise RuntimeError("Session factory was not initialised")
    return _session_factory


async def dispose_engine() -> None:
    """Close every pooled connection. Called when the application shuts down."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


# ── FastAPI dependency ────────────────────────────────────────────────────────


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield one session per request.

    Usage in a router:
        async def handler(session: AsyncSession = Depends(get_session)): ...
    """
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            # Undo anything not yet committed before the error propagates.
            await session.rollback()
            raise
        # Leaving the ``async with`` closes the session and returns its connection
        # to the pool; an uncommitted read-only transaction ends there.


async def ping_database() -> bool:
    """``SELECT 1`` through the pool. Used by the /health endpoint."""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Database health check failed")
        return False
    return True
