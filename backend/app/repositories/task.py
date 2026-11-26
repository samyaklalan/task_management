"""Database access for tasks. Never commits (see app/repositories/user.py).

Tasks are addressed by ``(project_id, number)``; the internal ``id`` never
leaves the API. Every read joins space, status and users in the same query
(the relationships are ``lazy="raise"``, so a forgotten join fails loudly).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import Select, case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, contains_eager

from app.models import Project, Space, Task, TaskComment, TaskStatus, User
from app.models.task import PRIORITY_RANK
from app.models.task_status import TaskStatusCategory
from app.schemas.common import Pagination, SortOrder
from app.schemas.task import TaskSort
from app.utils.pagination import PageParams, paginate
from app.utils.search import LIKE_ESCAPE, contains

_Assignee = aliased(User, name="assignee")
_Creator = aliased(User, name="creator")

_comment_count = (
    select(func.count())
    .where(TaskComment.task_id == Task.id)
    .correlate(Task)
    .scalar_subquery()
    .label("comment_count")
)

_priority_rank = case(PRIORITY_RANK, value=Task.priority)  # low 0 … urgent 3
_SORT_COLUMNS = {
    "created_at": Task.created_at,
    "updated_at": Task.updated_at,
    "number": Task.number,
    "priority": _priority_rank,
    "end_date": Task.end_date,
    "title": func.lower(Task.title),
}


def _with_refs(statement: Select[Any]) -> Select[Any]:
    """Join space, status and assignee and fill the relationships from the join."""
    return (
        statement.join(Task.space)
        .join(Task.status)
        .outerjoin(_Assignee, _Assignee.id == Task.assignee_id)
        .options(
            contains_eager(Task.space),
            contains_eager(Task.status),
            contains_eager(Task.assignee.of_type(_Assignee)),
        )
    )


# ── Numbering and defaults (TSK-2) ────────────────────────────────────────────


async def take_next_number(session: AsyncSession, project_id: int) -> int | None:
    """The task number, from one atomic UPDATE … RETURNING (api.md section 6).

    Simultaneous creates queue on the project row until each commits, so every
    task gets its own number and none is ever reused (the counter only goes up,
    even after a delete). ``updated_at`` is kept: a new task is not an edit of
    the project, so it mustn't make someone's project edit "stale".
    """
    return await session.scalar(
        update(Project)
        .where(Project.id == project_id)
        .values(
            next_task_number=Project.next_task_number + 1,
            updated_at=Project.updated_at,  # keep it: stops the onupdate default
        )
        .returning(Project.next_task_number - 1)
    )


async def default_status(session: AsyncSession, project_id: int) -> TaskStatus | None:
    """The first ``todo``-category status by position, else the first status."""
    is_todo = case((TaskStatus.category == TaskStatusCategory.TODO, 0), else_=1)
    return await session.scalar(
        select(TaskStatus)
        .where(TaskStatus.project_id == project_id)
        .order_by(is_todo, TaskStatus.position, TaskStatus.id)
        .limit(1)
    )


async def default_space(session: AsyncSession, project_id: int) -> Space | None:
    """The first space by position (normally "General", S-2)."""
    return await session.scalar(
        select(Space)
        .where(Space.project_id == project_id)
        .order_by(Space.position, Space.id)
        .limit(1)
    )


async def create(session: AsyncSession, **values: Any) -> Task:
    """IntegrityError: a status deleted a moment ago (fk_tasks_status_id_task_statuses)."""
    task = Task(**values)
    session.add(task)
    await session.flush()
    return task


# ── Reads ─────────────────────────────────────────────────────────────────────


async def get(session: AsyncSession, project_id: int, number: int) -> Task | None:
    """The bare task row (no relationships), for writes."""
    return await session.scalar(
        select(Task).where(Task.project_id == project_id, Task.number == number)
    )


async def get_detail(
    session: AsyncSession, project_id: int, number: int
) -> tuple[Task, int] | None:
    """TSK-3 in one query: the task with space, status, assignee, creator and
    its comment count. Returns ``(task, comment_count)``."""
    statement = (
        _with_refs(select(Task, _comment_count))
        .join(_Creator, _Creator.id == Task.created_by)
        .options(contains_eager(Task.creator.of_type(_Creator)))
        .where(Task.project_id == project_id, Task.number == number)
        .execution_options(populate_existing=True)
    )
    row = (await session.execute(statement)).first()
    return (row[0], row[1]) if row else None


async def list_tasks(
    session: AsyncSession,
    *,
    project_id: int,
    title_contains: str | None,
    key_number: int | None,
    space_id: int | None,
    status_id: int | None,
    category: TaskStatusCategory | None,
    assignee_id: int | None,
    unassigned: bool,
    priorities: list[str] | None,
    sort: TaskSort,
    order: SortOrder,
    page: PageParams,
) -> tuple[list[Task], Pagination]:
    """TSK-1: filter, search, sort and paginate in one query (plus the count).

    ``title_contains`` uses the trigram index (ix_tasks_title_trgm); a search
    that looks like this project's task key ("WEB-42") also matches that number.
    """
    statement = _with_refs(select(Task)).where(Task.project_id == project_id)

    if title_contains is not None:
        matches = Task.title.ilike(contains(title_contains), escape=LIKE_ESCAPE)
        if key_number is not None:
            matches = or_(matches, Task.number == key_number)
        statement = statement.where(matches)
    if space_id is not None:
        statement = statement.where(Task.space_id == space_id)
    if status_id is not None:
        statement = statement.where(Task.status_id == status_id)
    if category is not None:
        statement = statement.where(TaskStatus.category == category)
    if assignee_id is not None:
        statement = statement.where(Task.assignee_id == assignee_id)
    if unassigned:
        statement = statement.where(Task.assignee_id.is_(None))
    if priorities:
        statement = statement.where(Task.priority.in_(priorities))

    column = _SORT_COLUMNS[sort]
    if order == "desc":
        # The number breaks ties: unique per project, so pages never overlap.
        statement = statement.order_by(column.desc().nulls_last(), Task.number.desc())
    else:
        statement = statement.order_by(column.asc().nulls_last(), Task.number.asc())
    return await paginate(session, statement, page)


# ── Writes ────────────────────────────────────────────────────────────────────


async def update_if_unchanged(
    session: AsyncSession,
    task_id: int,
    *,
    expected_updated_at: datetime,
    values: dict[str, Any],
) -> bool:
    """TSK-4's optimistic update in one statement. False: someone saved (or
    deleted the task) since ``expected_updated_at``.
    IntegrityError: a status deleted a moment ago."""
    changed = await session.scalar(
        update(Task)
        .where(Task.id == task_id, Task.updated_at == expected_updated_at)
        .values(**values, updated_at=func.statement_timestamp())
        .returning(Task.id)
    )
    return changed is not None


async def delete_task(session: AsyncSession, project_id: int, number: int) -> bool:
    """TSK-8. Comments go with it (CASCADE). The number is never handed out again."""
    deleted = await session.scalar(
        delete(Task).where(Task.project_id == project_id, Task.number == number).returning(Task.id)
    )
    return deleted is not None
