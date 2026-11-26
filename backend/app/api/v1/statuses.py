"""STS-1 to STS-5: a project's task statuses, i.e. its board columns
(docs/api.md, section 12)."""

from typing import Annotated

from fastapi import APIRouter, Query
from fastapi import status as http_status

from app.api.deps import (
    ProjectContext,
    SessionDep,
    require_any_project_permission,
    require_project_permission,
)
from app.core.permissions import Action, Feature
from app.schemas.common import ErrorResponse, Id, IdPath
from app.schemas.task_status import (
    CreateStatusRequest,
    ReorderStatusesRequest,
    StatusList,
    StatusOut,
    UpdateStatusRequest,
)
from app.services import task_status as status_service

router = APIRouter(prefix="/projects/{public_id}/statuses", tags=["statuses"])

# The board needs its columns, so tasks.view is enough to read statuses.
View = Annotated[
    ProjectContext,
    require_any_project_permission(
        (Feature.TASK_STATUSES, Action.VIEW), (Feature.TASKS, Action.VIEW)
    ),
]
Create = Annotated[ProjectContext, require_project_permission(Feature.TASK_STATUSES, Action.CREATE)]
Edit = Annotated[ProjectContext, require_project_permission(Feature.TASK_STATUSES, Action.EDIT)]
Delete = Annotated[ProjectContext, require_project_permission(Feature.TASK_STATUSES, Action.DELETE)]

_READ_ERRORS = {403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}
_ARCHIVED = {409: {"model": ErrorResponse, "description": "project_archived"}}


@router.get(
    "",
    summary="Statuses in board order, with task counts (STS-1)",
    responses=_READ_ERRORS,
)
async def list_statuses(session: SessionDep, project: View) -> StatusList:
    return await status_service.list_statuses(session, project.project)


@router.post(
    "",
    status_code=http_status.HTTP_201_CREATED,
    summary="Add a status at the end of the board (STS-2)",
    responses=_READ_ERRORS | _ARCHIVED,
)
async def create_status(
    body: CreateStatusRequest, session: SessionDep, project: Create
) -> StatusOut:
    return await status_service.create_status(session, project.project, body)


# Declared before "/{status_id}" so "order" is never read as an id.
@router.put(
    "/order",
    summary="Set the whole board order (STS-4)",
    responses=_READ_ERRORS | _ARCHIVED,
)
async def reorder_statuses(
    body: ReorderStatusesRequest, session: SessionDep, project: Edit
) -> StatusList:
    return await status_service.reorder_statuses(session, project.project, body)


@router.patch(
    "/{status_id}",
    summary="Rename, recolour or change the category of a status (STS-3)",
    responses=_READ_ERRORS
    | {409: {"model": ErrorResponse, "description": "stale_update or project_archived"}},
)
async def update_status(
    status_id: IdPath, body: UpdateStatusRequest, session: SessionDep, project: Edit
) -> StatusOut:
    return await status_service.update_status(session, project.project, status_id, body)


@router.delete(
    "/{status_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    summary="Delete a status, optionally moving its tasks first (STS-5)",
    responses=_READ_ERRORS
    | {
        409: {
            "model": ErrorResponse,
            "description": "status_in_use, last_status or project_archived",
        }
    },
)
async def delete_status(
    status_id: IdPath,
    session: SessionDep,
    project: Delete,
    move_to: Annotated[
        Id | None, Query(description="Status that receives this status's tasks.")
    ] = None,
) -> None:
    await status_service.delete_status(session, project.project, status_id, move_to)
