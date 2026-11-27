"""Tasks (docs/api.md, TSK-1 to TSK-9)."""

import re
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    FieldValidationError,
    InvalidReferenceError,
    NotFoundError,
    StaleUpdateError,
)
from app.db.errors import constraint_name
from app.models import Project, Space, Task, TaskStatus
from app.models.task_status import TaskStatusCategory
from app.repositories import member as member_repository
from app.repositories import space as space_repository
from app.repositories import task as task_repository
from app.repositories import task_status as status_repository
from app.schemas.common import Page, SortOrder, UserRef
from app.schemas.task import (
    DATE_RANGE_MESSAGE,
    ChangeAssigneeRequest,
    ChangePriorityRequest,
    ChangeSpaceRequest,
    ChangeStatusRequest,
    CreateTaskRequest,
    SpaceRef,
    StatusRef,
    TaskDetail,
    TaskListItem,
    TaskSort,
    UpdateTaskRequest,
)
from app.utils.pagination import PageParams

_UNKNOWN_STATUS = "This status doesn't exist in this project."
_UNKNOWN_SPACE = "This space doesn't exist in this project."
_NO_ACCESS = "This user can't access this project."


# ── TSK-1 list ────────────────────────────────────────────────────────────────


async def list_tasks(
    session: AsyncSession,
    project: Project,
    *,
    q: str | None,
    space_id: int | None,
    status_id: int | None,
    category: TaskStatusCategory | None,
    assignee_id: int | None,
    unassigned: bool,
    priorities: list[str] | None,
    sort: TaskSort,
    order: SortOrder,
    page: PageParams,
) -> Page[TaskListItem]:
    if unassigned and assignee_id is not None:
        raise FieldValidationError("unassigned", "Use either assignee_id or unassigned=true.")
    tasks, pagination = await task_repository.list_tasks(
        session,
        project_id=project.id,
        title_contains=q,
        key_number=_key_number(project, q),
        space_id=space_id,
        status_id=status_id,
        category=category,
        assignee_id=assignee_id,
        unassigned=unassigned,
        priorities=priorities,
        sort=sort,
        order=order,
        page=page,
    )
    items = [TaskListItem(**_list_fields(project, task)) for task in tasks]
    return Page[TaskListItem](items=items, pagination=pagination)


def _key_number(project: Project, q: str | None) -> int | None:
    """ "WEB-42" (any case) in project WEB → 42, so a key search finds the task."""
    if q is None:
        return None
    match = re.fullmatch(rf"{re.escape(project.task_prefix)}-(\d{{1,9}})", q, re.IGNORECASE)
    return int(match.group(1)) if match else None


# ── TSK-2 create ──────────────────────────────────────────────────────────────


async def create_task(
    session: AsyncSession, project: Project, creator_id: int, data: CreateTaskRequest
) -> TaskDetail:
    """Validate the references, take the next number, insert: one transaction."""
    if data.status_id is None:
        task_status = await task_repository.default_status(session, project.id)
    else:
        task_status = await _status(session, project, data.status_id)
    if data.space_id is None:
        space = await task_repository.default_space(session, project.id)
    else:
        space = await _space(session, project, data.space_id)
    if task_status is None or space is None:  # pragma: no cover - last_status / S-5
        raise InvalidReferenceError("status_id", _UNKNOWN_STATUS)
    if data.assignee_id is not None:
        await _check_assignee(session, project, data.assignee_id)

    number = await task_repository.take_next_number(session, project.id)
    if number is None:  # pragma: no cover - the guard has just read the project
        raise NotFoundError("Project")
    try:
        async with session.begin_nested():
            await task_repository.create(
                session,
                project_id=project.id,
                number=number,
                title=data.title,
                description=data.description,
                space_id=space.id,
                status_id=task_status.id,
                assignee_id=data.assignee_id,
                created_by=creator_id,
                priority=data.priority,
                start_date=data.start_date,
                end_date=data.end_date,
            )
    except IntegrityError as exc:
        _raise_if_status_gone(exc)
        raise
    await session.commit()
    return await _detail(session, project, number)


# ── TSK-3 details ─────────────────────────────────────────────────────────────


async def get_task(session: AsyncSession, project: Project, number: int) -> TaskDetail:
    return await _detail(session, project, number)


# ── TSK-4 edit form ───────────────────────────────────────────────────────────

# ── TSK-4 edit form ───────────────────────────────────────────────────────────


async def update_task(
    session: AsyncSession, project: Project, number: int, data: UpdateTaskRequest
) -> TaskDetail:
    """Save the edit form, only if nobody saved in between (409 stale_update
    with the current task in ``details.current``)."""
    task = await task_repository.get(session, project.id, number)
    if task is None:
        raise NotFoundError("Task")
    if task.updated_at != data.expected_updated_at:
        raise StaleUpdateError(current=await _current(session, project, number))

    values = data.model_dump(include=data.model_fields_set - {"expected_updated_at"})
    if "status_id" in values:
        await _status(session, project, values["status_id"])
    if "space_id" in values:
        await _space(session, project, values["space_id"])
    if values.get("assignee_id") is not None:
        await _check_assignee(session, project, values["assignee_id"])
    _check_dates(task, values)

    if values:
        await _save(session, project, task, values, data.expected_updated_at)
    return await _detail(session, project, number)


# ── TSK-5 / TSK-6 / TSK-7 / TSK-9 quick actions ───────────────────────────────


async def change_status(
    session: AsyncSession, project: Project, number: int, data: ChangeStatusRequest
) -> TaskDetail:
    task = await _get_for_quick_action(session, project, number, data.expected_updated_at)
    await _status(session, project, data.status_id)
    return await _quick_save(session, project, task, {"status_id": data.status_id}, data)


async def change_assignee(
    session: AsyncSession, project: Project, number: int, data: ChangeAssigneeRequest
) -> TaskDetail:
    task = await _get_for_quick_action(session, project, number, data.expected_updated_at)
    if data.assignee_id is not None:
        await _check_assignee(session, project, data.assignee_id)
    return await _quick_save(session, project, task, {"assignee_id": data.assignee_id}, data)


async def change_priority(
    session: AsyncSession, project: Project, number: int, data: ChangePriorityRequest
) -> TaskDetail:
    task = await _get_for_quick_action(session, project, number, data.expected_updated_at)
    return await _quick_save(session, project, task, {"priority": data.priority}, data)


async def change_space(
    session: AsyncSession, project: Project, number: int, data: ChangeSpaceRequest
) -> TaskDetail:
    """The task keeps its number and key: numbers belong to the project (S-4)."""
    task = await _get_for_quick_action(session, project, number, data.expected_updated_at)
    await _space(session, project, data.space_id)
    return await _quick_save(session, project, task, {"space_id": data.space_id}, data)


async def _get_for_quick_action(
    session: AsyncSession,
    project: Project,
    number: int,
    expected_updated_at: datetime | None,
) -> Task:
    task = await task_repository.get(session, project.id, number)
    if task is None:
        raise NotFoundError("Task")
    if expected_updated_at is not None and task.updated_at != expected_updated_at:
        raise StaleUpdateError(current=await _current(session, project, number))
    return task


async def _quick_save(
    session: AsyncSession,
    project: Project,
    task: Task,
    values: dict[str, Any],
    data: (
        ChangeStatusRequest | ChangeAssigneeRequest | ChangePriorityRequest | ChangeSpaceRequest
    ),
) -> TaskDetail:
    """Write one field. Setting the value it already has writes nothing (a card
    dropped back on its own column doesn't change ``updated_at``, so nobody
    else's open edit form becomes stale)."""
    ((field, value),) = values.items()
    if getattr(task, field) != value:
        await _save(session, project, task, values, data.expected_updated_at)
    return await _detail(session, project, task.number)


# ── TSK-8 delete ──────────────────────────────────────────────────────────────


async def delete_task(session: AsyncSession, project: Project, number: int) -> None:
    if not await task_repository.delete_task(session, project.id, number):
        raise NotFoundError("Task")
    await session.commit()


async def _save(
    session: AsyncSession,
    project: Project,
    task: Task,
    values: dict[str, Any],
    expected_updated_at: datetime | None,
) -> None:
    """The UPDATE shared by TSK-4 and the quick actions, then commit."""
    number = task.number  # read before a possible failure leaves `task` expired
    try:
        async with session.begin_nested():
            saved = await task_repository.update_if_unchanged(
                session, task.id, expected_updated_at=expected_updated_at, values=values
            )
    except IntegrityError as exc:
        _raise_if_status_gone(exc)
        raise
    if not saved:
        if expected_updated_at is None:  # last write wins, so no match = deleted
            raise NotFoundError("Task")
        # Saved or deleted by someone else since our SELECT (404 if deleted).
        raise StaleUpdateError(current=await _current(session, project, number))
    await session.commit()


# ── Checks ────────────────────────────────────────────────────────────────────


async def _status(session: AsyncSession, project: Project, status_id: int) -> TaskStatus:
    task_status = await status_repository.get(session, project.id, status_id)
    if task_status is None:
        raise InvalidReferenceError("status_id", _UNKNOWN_STATUS)
    return task_status


async def _space(session: AsyncSession, project: Project, space_id: int) -> Space:
    space = await space_repository.get(session, project.id, space_id)
    if space is None:
        raise InvalidReferenceError("space_id", _UNKNOWN_SPACE)
    return space


async def _check_assignee(session: AsyncSession, project: Project, user_id: int) -> None:
    """Same access rule and lock as TEAM-7: a simultaneous "remove member"
    (MEM-4) waits for us and then unassigns this task too (A-7)."""
    with_access = await member_repository.lock_users_with_access(
        session,
        organization_id=project.organization_id,
        project_id=project.id,
        user_ids=[user_id],
    )
    if user_id not in with_access:
        raise InvalidReferenceError("assignee_id", _NO_ACCESS)


def _check_dates(task: Task, values: dict) -> None:
    """The schema checks the dates when both are sent; one date alone is
    checked against the stored other one (``task`` is the expected version)."""
    start = values.get("start_date", task.start_date)
    end = values.get("end_date", task.end_date)
    if start is not None and end is not None and end < start:
        raise FieldValidationError("end_date", DATE_RANGE_MESSAGE)


def _raise_if_status_gone(exc: IntegrityError) -> None:
    """The status was deleted between our check and our write (STS-5)."""
    if constraint_name(exc) == "fk_tasks_status_id_task_statuses":
        raise InvalidReferenceError("status_id", _UNKNOWN_STATUS) from exc


# ── Responses ─────────────────────────────────────────────────────────────────


async def _detail(session: AsyncSession, project: Project, number: int) -> TaskDetail:
    found = await task_repository.get_detail(session, project.id, number)
    if found is None:
        raise NotFoundError("Task")
    task, comment_count = found
    return TaskDetail(
        **_list_fields(project, task),
        description=task.description,
        created_by=UserRef(id=task.creator.id, name=task.creator.name),
        comment_count=comment_count,
    )


async def _current(session: AsyncSession, project: Project, number: int) -> dict:
    """The task as TSK-3 shows it, for ``details.current`` of a 409 (or 404 if
    it was deleted meanwhile)."""
    return (await _detail(session, project, number)).model_dump(mode="json")


def _list_fields(project: Project, task: Task) -> dict:
    return {
        "number": task.number,
        "key": f"{project.task_prefix}-{task.number}",
        "title": task.title,
        "priority": task.priority,
        "space": SpaceRef(id=task.space.id, name=task.space.name),
        "status": StatusRef(
            id=task.status.id,
            name=task.status.name,
            color=task.status.color,
            category=task.status.category,
        ),
        "assignee": (
            UserRef(id=task.assignee.id, name=task.assignee.name) if task.assignee else None
        ),
        "start_date": task.start_date,
        "end_date": task.end_date,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }
