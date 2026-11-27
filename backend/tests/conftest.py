"""Shared pytest fixtures: the test database, one rolled-back session per test.

How a test run works:

1. ``engine`` (once per run) upgrades the TEST database to the latest migration
   with Alembic, so tests always run against the real schema that production
   gets, not a ``create_all()`` copy. It then opens one engine for the whole run.
2. ``db_session`` (once per test) opens a connection, starts a transaction and
   gives the test a session inside it. When the test ends the transaction is
   rolled back, so every test starts with an empty database and tests can't
   affect each other, without truncating tables.

``session.commit()`` still works inside a test (services call it): the session
uses ``join_transaction_mode="create_savepoint"``, so a commit only releases a
SAVEPOINT and the outer rollback still undoes everything. After an expected
``IntegrityError``, call ``await db_session.rollback()``: it rolls back to the
last savepoint and the session is usable again.

Safety: tests only ever use TEST_DATABASE_URL. Settings (app/core/config.py)
already refuses a test database that is the development database or whose
name doesn't end in "_test"; here the run stops if TEST_DATABASE_URL is missing.

Async tests run on AnyIO's pytest plugin (``anyio_mode = "auto"`` in
pyproject.toml): write ``async def test_...`` and it just runs.
"""

import argparse
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from anyio import to_thread
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import get_settings
from app.db.session import create_engine

BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Run every async test and fixture on asyncio (the backend uvicorn uses).

    Session scope lets session-scoped async fixtures (``engine``) share one
    event loop with all tests, which asyncpg connections require.
    """
    return "asyncio"


def _test_database_url() -> str:
    settings = get_settings()
    if settings.test_database_url is None:
        pytest.exit(
            "TEST_DATABASE_URL is not set in backend/.env. "
            "Tests never run against the development database.",
            returncode=2,
        )
    return settings.test_database_url.get_secret_value()


def _upgrade_test_database() -> None:
    """``alembic -x db=test upgrade head``, called from Python."""
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.cmd_opts = argparse.Namespace(x=["db=test"])
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    """One engine on the test database for the whole run, schema at head."""
    url = _test_database_url()
    # alembic/env.py runs its own event loop (asyncio.run), which can't start
    # inside this one, so the upgrade runs in a worker thread.
    await to_thread.run_sync(_upgrade_test_database)

    test_engine = create_engine(url=url)
    try:
        yield test_engine
    finally:
        await test_engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session whose changes are all rolled back when the test ends."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,  # same as the app's sessions (app/db/session.py)
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
