"""Alembic migration environment (async).

Alembic runs this file for every command (``upgrade``, ``downgrade``, ``current``,
``check``, ``revision --autogenerate``). It:

1. reads the database URL from ``app/core/config.py`` (never from alembic.ini);
2. imports ``app.models`` so all 14 tables are registered on ``Base.metadata``,
   which ``--autogenerate`` and ``alembic check`` compare with the database;
3. connects with the async driver (asyncpg) and runs the migrations inside one
   transaction. PostgreSQL supports transactional DDL, so a failed migration is
   rolled back completely.

Choose the database with ``-x db=...``:
    alembic upgrade head              -> development database (DATABASE_URL)
    alembic -x db=test upgrade head   -> test database (TEST_DATABASE_URL)
"""

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401  (registers all 14 tables on Base.metadata)
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.base import Base

settings = get_settings()
configure_logging(settings)

# The metadata Alembic compares against the database.
target_metadata = Base.metadata


def get_url() -> str:
    """Database URL for this run, chosen with ``-x db=dev`` (default) or ``-x db=test``."""
    target = context.get_x_argument(as_dictionary=True).get("db", "dev")
    if target == "dev":
        return settings.database_url.get_secret_value()
    if target == "test":
        if settings.test_database_url is None:
            raise RuntimeError("TEST_DATABASE_URL is not set in backend/.env")
        return settings.test_database_url.get_secret_value()
    raise RuntimeError(f"Unknown -x db={target!r}; use 'dev' or 'test'")


def run_migrations_offline() -> None:
    """``alembic upgrade head --sql``: print the SQL instead of running it."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Also detect column type changes (e.g. VARCHAR(100) -> VARCHAR(150)),
        # not only added or removed columns.
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # A separate, short-lived engine, not the application's pool (app/db/session.py):
    # * NullPool: one connection, closed when the migration finishes;
    # * no 30 s statement_timeout: building an index on a large table may
    #   legitimately take longer than an API query is allowed to.
    engine = create_async_engine(
        get_url(),
        poolclass=pool.NullPool,
        connect_args={
            "server_settings": {
                "application_name": "task_management_migrations",
                "timezone": "UTC",
            }
        },
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
