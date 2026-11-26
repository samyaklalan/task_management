"""Database access for spaces. Never commits (see app/repositories/user.py)."""

from datetime import datetime
from typing import Any

from sqlalchemy import Row, case, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Space, Task
from app.schemas.common import Pagination
from app.utils.pagination import PageParams, paginate
from app.utils.search import LIKE_ESCAPE, contains

# Tasks per space, inside the same SELECT (no N+1 over a page).
_task_count = (
    select(func.count())
    .where(Task.space_id == Space.id)
    .correlate(Space)
    .scalar_subquery()
    .label("task_count")
)


async def lock_layout(session: AsyncSession, project_id: int) -> None:
    """Serialize "add a space" and "reorder" within one project, until commit.

    A transaction-level advisory lock on a key derived from the project id: two
    simultaneous creates can't both take the same ``max(position) + 1``, and a
    create can't slip in between a reorder's check and its update. Other writes
    in the project (tasks, renames) don't take it, so they never wait on it.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended('spaces:' || :project_id, 0))"),
        {"project_id": str(project_id)},
    )


async def create(
    session: AsyncSession,
    *,
    project_id: int,
    name: str,
    position: int,
    description: str | None = None,
) -> Space:
    """IntegrityError: uq_spaces_project_id_name (name already used here)."""
    space = Space(project_id=project_id, name=name, description=description, position=position)
    session.add(space)
    await session.flush()
    return space


async def next_position(session: AsyncSession, project_id: int) -> int:
    """``max(position) + 1`` (0 for an empty project). Call under lock_layout."""
    highest = await session.scalar(
        select(func.max(Space.position)).where(Space.project_id == project_id)
    )
    return 0 if highest is None else highest + 1


async def list_spaces(
    session: AsyncSession, *, project_id: int, q: str | None, page: PageParams
) -> tuple[list[Row[tuple[Space, int]]], Pagination]:
    """SPC-1: sidebar order (position, then id), task counts included."""
    statement: Any = (
        select(Space, _task_count)
        .where(Space.project_id == project_id)
        .order_by(Space.position, Space.id)
    )
    if q is not None:
        statement = statement.where(Space.name.ilike(contains(q), escape=LIKE_ESCAPE))
    return await paginate(session, statement, page, scalars=False)


async def get_with_count(
    session: AsyncSession, project_id: int, space_id: int
) -> Row[tuple[Space, int]] | None:
    """A space of THIS project (another project's space is "not found")."""
    result = await session.execute(
        select(Space, _task_count).where(Space.id == space_id, Space.project_id == project_id)
    )
    return result.first()


async def get(session: AsyncSession, project_id: int, space_id: int) -> Space | None:
    return await session.scalar(
        select(Space).where(Space.id == space_id, Space.project_id == project_id)
    )


async def count_tasks(session: AsyncSession, space_id: int) -> int:
    count = await session.scalar(
        select(func.count()).select_from(Task).where(Task.space_id == space_id)
    )
    return count or 0


async def update_if_unchanged(
    session: AsyncSession,
    space_id: int,
    *,
    expected_updated_at: datetime,
    values: dict[str, Any],
) -> Space | None:
    """Optimistic update. ``None``: someone saved in between.
    IntegrityError: uq_spaces_project_id_name."""
    return await session.scalar(
        update(Space)
        .where(Space.id == space_id, Space.updated_at == expected_updated_at)
        .values(**values, updated_at=func.statement_timestamp())
        .returning(Space)
        .execution_options(populate_existing=True)
    )


async def lock_positions(session: AsyncSession, project_id: int) -> dict[int, int]:
    """SPC-5: every space of the project, ``{id: position}``, locked FOR UPDATE
    in ascending id order (the same order everywhere, so no deadlocks)."""
    rows = await session.execute(
        select(Space.id, Space.position)
        .where(Space.project_id == project_id)
        .order_by(Space.id)
        .with_for_update()
    )
    return {space_id: position for space_id, position in rows}


async def set_positions(session: AsyncSession, positions: dict[int, int]) -> None:
    """One UPDATE for all changed rows (``{id: new_position}``).

    ``updated_at`` is left alone on purpose: the order is not part of what
    SPC-4 edits, so a reorder must not make someone's rename "stale".
    """
    if not positions:
        return
    await session.execute(
        update(Space)
        .where(Space.id.in_(positions))
        .values(
            position=case(positions, value=Space.id),
            updated_at=Space.updated_at,  # keep it: stops the onupdate default
        )
    )
