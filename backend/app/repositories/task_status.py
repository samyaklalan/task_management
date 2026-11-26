"""Database access for task statuses. Never commits (see app/repositories/user.py)."""

from datetime import datetime
from typing import Any

from sqlalchemy import Row, case, delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task, TaskStatus
from app.models.task_status import DEFAULT_TASK_STATUSES

# Tasks per status, inside the same SELECT (no N+1).
_task_count = (
    select(func.count())
    .where(Task.status_id == TaskStatus.id)
    .correlate(TaskStatus)
    .scalar_subquery()
    .label("task_count")
)


async def create_defaults(session: AsyncSession, project_id: int) -> None:
    """To Do, In Progress, Done at positions 0, 1, 2 (A-1), in one INSERT."""
    await session.execute(
        insert(TaskStatus),
        [
            {
                "project_id": project_id,
                "name": name,
                "color": color,
                "category": category.value,
                "position": position,
            }
            for position, (name, color, category) in enumerate(DEFAULT_TASK_STATUSES)
        ],
    )


async def lock_board(session: AsyncSession, project_id: int) -> None:
    """Serialize add, reorder and delete of a project's statuses until commit
    (a transaction-level advisory lock, like spaces' lock_layout).

    It is what makes "max(position) + 1", "every status exactly once" and
    "never delete the last status" hold when two people act at the same time.
    Task writes don't take it.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended('statuses:' || :project_id, 0))"),
        {"project_id": str(project_id)},
    )


async def next_position(session: AsyncSession, project_id: int) -> int:
    """``max(position) + 1`` (0 if none). Call under lock_board."""
    highest = await session.scalar(
        select(func.max(TaskStatus.position)).where(TaskStatus.project_id == project_id)
    )
    return 0 if highest is None else highest + 1


async def create(
    session: AsyncSession,
    *,
    project_id: int,
    name: str,
    color: str,
    category: str,
    position: int,
) -> TaskStatus:
    task_status = TaskStatus(
        project_id=project_id,
        name=name,
        color=color,
        category=category,
        position=position,
    )
    session.add(task_status)
    await session.flush()
    return task_status


async def list_with_counts(
    session: AsyncSession, project_id: int
) -> list[Row[tuple[TaskStatus, int]]]:
    """STS-1: board order (position, then id), task counts included. One query."""
    rows = await session.execute(
        select(TaskStatus, _task_count)
        .where(TaskStatus.project_id == project_id)
        .order_by(TaskStatus.position, TaskStatus.id)
    )
    return list(rows)


async def get(session: AsyncSession, project_id: int, status_id: int) -> TaskStatus | None:
    """A status of THIS project (another project's status is "not found")."""
    return await session.scalar(
        select(TaskStatus).where(TaskStatus.id == status_id, TaskStatus.project_id == project_id)
    )


async def lock(session: AsyncSession, project_id: int, status_id: int) -> TaskStatus | None:
    """Like ``get``, locked ``FOR UPDATE`` until commit. While it is held, no
    task can be created in or moved into this status (their foreign key check
    needs a share lock on the row), so a count taken now stays true."""
    return await session.scalar(
        select(TaskStatus)
        .where(TaskStatus.id == status_id, TaskStatus.project_id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def count_tasks(session: AsyncSession, status_id: int) -> int:
    count = await session.scalar(
        select(func.count()).select_from(Task).where(Task.status_id == status_id)
    )
    return count or 0


async def count_statuses(session: AsyncSession, project_id: int) -> int:
    count = await session.scalar(
        select(func.count()).select_from(TaskStatus).where(TaskStatus.project_id == project_id)
    )
    return count or 0


async def update_if_unchanged(
    session: AsyncSession,
    status_id: int,
    *,
    expected_updated_at: datetime,
    values: dict[str, Any],
) -> TaskStatus | None:
    """Optimistic update. ``None``: someone saved (or deleted it) in between."""
    return await session.scalar(
        update(TaskStatus)
        .where(TaskStatus.id == status_id, TaskStatus.updated_at == expected_updated_at)
        .values(**values, updated_at=func.statement_timestamp())
        .returning(TaskStatus)
        .execution_options(populate_existing=True)
    )


async def lock_positions(session: AsyncSession, project_id: int) -> dict[int, int]:
    """STS-4: every status of the project, ``{id: position}``, locked FOR UPDATE
    in ascending id order (the same order everywhere, so no deadlocks)."""
    rows = await session.execute(
        select(TaskStatus.id, TaskStatus.position)
        .where(TaskStatus.project_id == project_id)
        .order_by(TaskStatus.id)
        .with_for_update()
    )
    return {status_id: position for status_id, position in rows}


async def set_positions(session: AsyncSession, positions: dict[int, int]) -> None:
    """One UPDATE for all changed rows. ``updated_at`` is kept on purpose: the
    board order isn't part of what STS-3 edits (same rule as spaces)."""
    if not positions:
        return
    await session.execute(
        update(TaskStatus)
        .where(TaskStatus.id.in_(positions))
        .values(
            position=case(positions, value=TaskStatus.id),
            updated_at=TaskStatus.updated_at,  # keep it: stops the onupdate default
        )
    )


async def move_tasks(session: AsyncSession, *, from_status_id: int, to_status_id: int) -> None:
    """STS-5: every task of one status moves to another, in one UPDATE.
    ``updated_at`` moves on, so anyone editing one of them gets ``stale_update``."""
    await session.execute(
        update(Task)
        .where(Task.status_id == from_status_id)
        .values(status_id=to_status_id, updated_at=func.statement_timestamp())
    )


async def delete_status(session: AsyncSession, status_id: int) -> None:
    """IntegrityError (fk_tasks_status_id_task_statuses) if a task still uses it."""
    await session.execute(delete(TaskStatus).where(TaskStatus.id == status_id))
