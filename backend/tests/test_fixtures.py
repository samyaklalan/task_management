"""Checks that the test setup itself works (B19).

If one of these fails, every other test's result is meaningless, so they cover:
the schema is migrated, each test is rolled back, a commit inside a test is
safe, the session recovers after a constraint error, the factories make valid
rows, and the query counter counts only real queries.
"""

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import Organization, Project
from tests.conftest import BACKEND_DIR
from tests.factories import make_organization, make_project, make_task
from tests.utils.query_counter import count_queries

ROLLBACK_CHECK_SLUG = "rollback-check"


async def test_test_database_is_migrated_to_head(db_session: AsyncSession) -> None:
    head = ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini"))).get_current_head()
    current = await db_session.scalar(text("SELECT version_num FROM alembic_version"))
    database = await db_session.scalar(text("SELECT current_database()"))

    assert current == head
    assert database.endswith("_test")


async def test_commit_inside_a_test_is_still_rolled_back_part_1(db_session: AsyncSession) -> None:
    await make_organization(db_session, slug=ROLLBACK_CHECK_SLUG)
    await db_session.commit()  # services commit; here that only releases a savepoint

    assert await db_session.scalar(select(func.count()).select_from(Organization)) == 1


async def test_commit_inside_a_test_is_still_rolled_back_part_2(db_session: AsyncSession) -> None:
    # Runs after part 1 (pytest keeps file order): its row must be gone.
    assert await db_session.scalar(select(func.count()).select_from(Organization)) == 0


async def test_session_is_usable_after_an_integrity_error(db_session: AsyncSession) -> None:
    await make_organization(db_session, slug="taken")
    await db_session.commit()

    with pytest.raises(IntegrityError, match="uq_organizations_slug"):
        await make_organization(db_session, slug="taken")
    await db_session.rollback()  # back to the last savepoint

    # The committed row survived; only the failed insert was undone.
    slugs = (await db_session.scalars(select(Organization.slug))).all()
    assert slugs == ["taken"]


async def test_factories_build_a_valid_task_with_numbers_in_sequence(
    db_session: AsyncSession,
) -> None:
    project = await make_project(db_session)
    first = await make_task(db_session, project=project)
    second = await make_task(db_session, project=project)

    assert (first.number, second.number) == (1, 2)
    next_number = await db_session.scalar(
        select(Project.next_task_number).where(Project.id == project.id)
    )
    assert next_number == 3
    assert first.created_at is not None and first.priority == "medium"  # server defaults


async def test_query_counter_ignores_savepoints(
    engine: AsyncEngine, db_session: AsyncSession
) -> None:
    await db_session.execute(text("SELECT 1"))  # connection already open before counting

    with count_queries(engine) as queries:
        await db_session.execute(select(Organization).limit(1))
        await make_organization(db_session)
        await db_session.commit()  # RELEASE SAVEPOINT: not counted

    assert queries.count == 2, str(queries)
    assert queries.statements[0].lstrip().startswith("SELECT")
    assert queries.statements[1].lstrip().startswith("INSERT INTO organizations")
