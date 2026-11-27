"""Task statuses: the board columns of a project (docs/api.md, STS-1 to STS-5)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    InvalidReferenceError,
    LastStatusError,
    NotFoundError,
    StaleUpdateError,
    StatusInUseError,
)
from app.models import Project, TaskStatus
from app.repositories import task_status as status_repository
from app.schemas.task_status import (
    CreateStatusRequest,
    ReorderStatusesRequest,
    StatusList,
    StatusOut,
    UpdateStatusRequest,
)


async def list_statuses(session: AsyncSession, project: Project) -> StatusList:
    rows = await status_repository.list_with_counts(session, project.id)
    return StatusList(items=[_out(task_status, count) for task_status, count in rows])


async def create_status(
    session: AsyncSession, project: Project, data: CreateStatusRequest
) -> StatusOut:
    """At the end of the board (max position + 1, under the board lock)."""
    await status_repository.lock_board(session, project.id)
    position = await status_repository.next_position(session, project.id)
    task_status = await status_repository.create(
        session,
        project_id=project.id,
        name=data.name,
        color=data.color,
        category=data.category.value,
        position=position,
    )
    await session.commit()
    return _out(task_status, 0)


async def update_status(
    session: AsyncSession, project: Project, status_id: int, data: UpdateStatusRequest
) -> StatusOut:
    """Rename, recolour and/or change category. Tasks keep their status_id, so
    they follow automatically."""
    task_status = await _get(session, project, status_id)
    if task_status.updated_at != data.expected_updated_at:
        raise StaleUpdateError(current=await _current(session, task_status))

    values = data.model_dump(mode="json", include=data.model_fields_set - {"expected_updated_at"})
    if values:
        updated = await status_repository.update_if_unchanged(
            session,
            task_status.id,
            expected_updated_at=data.expected_updated_at,
            values=values,
        )
        if updated is None:  # saved or deleted by someone else since our SELECT
            current = await _get(session, project, status_id)
            raise StaleUpdateError(current=await _current(session, current))
        task_status = updated
        await session.commit()
    return _out(task_status, await status_repository.count_tasks(session, task_status.id))


async def reorder_statuses(
    session: AsyncSession, project: Project, data: ReorderStatusesRequest
) -> StatusList:
    """STS-4: positions become 0, 1, 2, … in the order given. Only changed rows
    are written, so sending the same order twice is a no-op."""
    await status_repository.lock_board(session, project.id)
    current = await status_repository.lock_positions(session, project.id)

    duplicates = len(data.status_ids) != len(set(data.status_ids))
    if duplicates or set(data.status_ids) != set(current):
        missing = sorted(set(current) - set(data.status_ids))
        unknown = sorted(set(data.status_ids) - set(current))
        problems = [
            f"missing: {', '.join(map(str, missing))}" if missing else "",
            f"not in this project: {', '.join(map(str, unknown))}" if unknown else "",
            "an id appears more than once" if duplicates else "",
        ]
        raise InvalidReferenceError(
            "status_ids",
            "status_ids must list every status of this project exactly once ("
            + "; ".join(p for p in problems if p)
            + ").",
        )

    changed = {
        status_id: position
        for position, status_id in enumerate(data.status_ids)
        if current[status_id] != position
    }
    await status_repository.set_positions(session, changed)
    await session.commit()
    return await list_statuses(session, project)


async def delete_status(
    session: AsyncSession, project: Project, status_id: int, move_to: int | None
) -> None:
    """STS-5. Without ``move_to`` only an unused status can go (409
    ``status_in_use`` with the task count). With it, the tasks move and the
    status is deleted in one transaction. The last status never goes.

    Locks: the board lock (so two deletes can't remove the last two statuses at
    once), then the status row FOR UPDATE (so no task can land in it while we
    count, move and delete).
    """
    await status_repository.lock_board(session, project.id)
    task_status = await status_repository.lock(session, project.id, status_id)
    if task_status is None:
        raise NotFoundError("Status")
    if await status_repository.count_statuses(session, project.id) == 1:
        raise LastStatusError()

    if move_to is not None:
        target = await status_repository.get(session, project.id, move_to)
        if target is None or target.id == task_status.id:
            raise InvalidReferenceError(
                "move_to", "move_to must be another status of this project."
            )
        await status_repository.move_tasks(
            session, from_status_id=task_status.id, to_status_id=target.id
        )
    else:
        task_count = await status_repository.count_tasks(session, task_status.id)
        if task_count:
            raise StatusInUseError(task_count)

    await status_repository.delete_status(session, task_status.id)
    await session.commit()


# ── Shared ────────────────────────────────────────────────────────────────────


async def _get(session: AsyncSession, project: Project, status_id: int) -> TaskStatus:
    task_status = await status_repository.get(session, project.id, status_id)
    if task_status is None:
        raise NotFoundError("Status")
    return task_status


async def _current(session: AsyncSession, task_status: TaskStatus) -> dict:
    count = await status_repository.count_tasks(session, task_status.id)
    return _out(task_status, count).model_dump(mode="json")


def _out(task_status: TaskStatus, task_count: int) -> StatusOut:
    return StatusOut(
        id=task_status.id,
        name=task_status.name,
        color=task_status.color,
        category=task_status.category,
        position=task_status.position,
        task_count=task_count,
        updated_at=task_status.updated_at,
    )
