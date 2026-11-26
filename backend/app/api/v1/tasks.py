"""TSK-1 to TSK-9: a project's tasks (docs/api.md, section 12).

Tasks are addressed by their number (``/tasks/42`` is WEB-42); the internal id
never appears in the API.
"""

from typing import Annotated

from fastapi import APIRouter, Query
from fastapi import status as http_status

from app.api.deps import (
    CurrentUserDep,
    ProjectContext,
    SessionDep,
    require_project_permission,
)
from app.core.permissions import Action, Feature
from app.models.task import TaskPriority
from app.models.task_status import TaskStatusCategory
from app.schemas.common import ErrorResponse, Id, Page, SortOrder
from app.schemas.task import (
    ChangeAssigneeRequest,
    ChangePriorityRequest,
    ChangeSpaceRequest,
    ChangeStatusRequest,
    CreateTaskRequest,
    TaskDetail,
    TaskListItem,
    TaskNumber,
    TaskSort,
    UpdateTaskRequest,
)
from app.services import task as task_service
from app.utils.pagination import PageQuery
from app.utils.search import SearchQuery

router = APIRouter(prefix="/projects/{public_id}/tasks", tags=["tasks"])

View = Annotated[ProjectContext, require_project_permission(Feature.TASKS, Action.VIEW)]
Create = Annotated[ProjectContext, require_project_permission(Feature.TASKS, Action.CREATE)]
Edit = Annotated[ProjectContext, require_project_permission(Feature.TASKS, Action.EDIT)]
Delete = Annotated[ProjectContext, require_project_permission(Feature.TASKS, Action.DELETE)]


_READ_ERRORS = {403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}
_WRITE_ERRORS = _READ_ERRORS | {409: {"model": ErrorResponse, "description": "project_archived"}}


@router.get(
    "",
    summary="Task list and board: filter, search, sort, paginate (TSK-1)",
    responses=_READ_ERRORS,
)
async def list_tasks(
    session: SessionDep,
    project: View,
    page: PageQuery,
    q: SearchQuery,
    space_id: Annotated[Id | None, Query()] = None,
    status_id: Annotated[Id | None, Query()] = None,
    category: TaskStatusCategory | None = None,
    assignee_id: Annotated[Id | None, Query()] = None,
    unassigned: bool = False,
    priority: Annotated[
        list[TaskPriority] | None,
        Query(description="Repeatable: priority=high&priority=urgent"),
    ] = None,
    sort: TaskSort = "created_at",
    order: SortOrder = "desc",
) -> Page[TaskListItem]:
    return await task_service.list_tasks(
        session,
        project.project,
        q=q,
        space_id=space_id,
        status_id=status_id,
        category=category,
        assignee_id=assignee_id,
        unassigned=unassigned,
        priorities=[p.value for p in priority] if priority else None,
        sort=sort,
        order=order,
        page=page,
    )


@router.post(
    "",
    status_code=http_status.HTTP_201_CREATED,
    summary="Create a task; it gets the project's next number (TSK-2)",
    responses=_WRITE_ERRORS,
)
async def create_task(
    body: CreateTaskRequest, session: SessionDep, user: CurrentUserDep, project: Create
) -> TaskDetail:
    return await task_service.create_task(session, project.project, user.id, body)


@router.get("/{number}", summary="Task details (TSK-3)", responses=_READ_ERRORS)
async def get_task(number: TaskNumber, session: SessionDep, project: View) -> TaskDetail:
    return await task_service.get_task(session, project.project, number)


@router.patch(
    "/{number}",
    summary="Save the task edit form (TSK-4)",
    responses=_READ_ERRORS
    | {409: {"model": ErrorResponse, "description": "stale_update or project_archived"}},
)
async def update_task(
    number: TaskNumber, body: UpdateTaskRequest, session: SessionDep, project: Edit
) -> TaskDetail:
    return await task_service.update_task(session, project.project, number, body)


@router.delete(
    "/{number}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    summary="Delete a task and its comments; the number is never reused (TSK-8)",
    responses=_WRITE_ERRORS,
)
async def delete_task(number: TaskNumber, session: SessionDep, project: Delete) -> None:
    await task_service.delete_task(session, project.project, number)


# ── Quick actions (TSK-5, TSK-6, TSK-7, TSK-9) ────────────────────────────────

_QUICK_ERRORS = _READ_ERRORS | {
    409: {
        "model": ErrorResponse,
        "description": "stale_update (only if expected_updated_at was sent) or project_archived",
    }
}


@router.patch(
    "/{number}/status",
    summary="Move to another status (TSK-5)",
    responses=_QUICK_ERRORS,
)
async def change_status(
    number: TaskNumber, body: ChangeStatusRequest, session: SessionDep, project: Edit
) -> TaskDetail:
    return await task_service.change_status(session, project.project, number, body)


@router.patch(
    "/{number}/assignee",
    summary="Assign, reassign or unassign (null) (TSK-6)",
    responses=_QUICK_ERRORS,
)
async def change_assignee(
    number: TaskNumber, body: ChangeAssigneeRequest, session: SessionDep, project: Edit
) -> TaskDetail:
    return await task_service.change_assignee(session, project.project, number, body)


@router.patch("/{number}/priority", summary="Change priority (TSK-7)", responses=_QUICK_ERRORS)
async def change_priority(
    number: TaskNumber, body: ChangePriorityRequest, session: SessionDep, project: Edit
) -> TaskDetail:
    return await task_service.change_priority(session, project.project, number, body)


@router.patch(
    "/{number}/space",
    summary="Move to another space; the number stays (TSK-9)",
    responses=_QUICK_ERRORS,
)
async def change_space(
    number: TaskNumber, body: ChangeSpaceRequest, session: SessionDep, project: Edit
) -> TaskDetail:
    return await task_service.change_space(session, project.project, number, body)
