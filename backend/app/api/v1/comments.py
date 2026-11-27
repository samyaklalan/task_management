"""CMT-1 to CMT-4: the comments on a task (docs/api.md, section 12), with the
author rules of A-6."""

from typing import Annotated

from fastapi import APIRouter
from fastapi import status as http_status

from app.api.deps import (
    CurrentUserDep,
    ProjectContext,
    SessionDep,
    require_any_project_permission,
    require_project_permission,
)
from app.core.permissions import Action, Feature
from app.schemas.comment import CommentOut, CreateCommentRequest, UpdateCommentRequest
from app.schemas.common import ErrorResponse, IdPath, Page, SortOrder
from app.schemas.task import TaskNumber
from app.services import comment as comment_service
from app.utils.pagination import PageQuery

router = APIRouter(prefix="/projects/{public_id}/tasks/{number}/comments", tags=["comments"])

View = Annotated[ProjectContext, require_project_permission(Feature.COMMENTS, Action.VIEW)]
Create = Annotated[ProjectContext, require_project_permission(Feature.COMMENTS, Action.CREATE)]
# Editing is for the author (checked in the service), whose role needs comments.edit.
Edit = Annotated[ProjectContext, require_project_permission(Feature.COMMENTS, Action.EDIT)]
# Deleting: the author (with comments.edit) or a moderator (comments.delete).
Delete = Annotated[
    ProjectContext,
    require_any_project_permission(
        (Feature.COMMENTS, Action.EDIT), (Feature.COMMENTS, Action.DELETE)
    ),
]

_READ_ERRORS = {403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}
_WRITE_ERRORS = _READ_ERRORS | {409: {"model": ErrorResponse, "description": "project_archived"}}


@router.get("", summary="A task's comments, oldest first (CMT-1)", responses=_READ_ERRORS)
async def list_comments(
    number: TaskNumber,
    session: SessionDep,
    project: View,
    page: PageQuery,
    order: SortOrder = "asc",
) -> Page[CommentOut]:
    return await comment_service.list_comments(
        session, project.project, number, order=order, page=page
    )


@router.post(
    "",
    status_code=http_status.HTTP_201_CREATED,
    summary="Add a comment as the current user (CMT-2)",
    responses=_WRITE_ERRORS,
)
async def add_comment(
    number: TaskNumber,
    body: CreateCommentRequest,
    session: SessionDep,
    user: CurrentUserDep,
    project: Create,
) -> CommentOut:
    return await comment_service.add_comment(session, project.project, number, user.id, body)


@router.patch(
    "/{comment_id}",
    summary="Edit your own comment (CMT-3)",
    responses=_READ_ERRORS
    | {409: {"model": ErrorResponse, "description": "stale_update or project_archived"}},
)
async def edit_comment(
    number: TaskNumber,
    comment_id: IdPath,
    body: UpdateCommentRequest,
    session: SessionDep,
    user: CurrentUserDep,
    project: Edit,
) -> CommentOut:
    return await comment_service.edit_comment(
        session, project.project, number, comment_id, user.id, body
    )


@router.delete(
    "/{comment_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    summary="Delete your own comment, or any comment as a moderator (CMT-4)",
    responses=_WRITE_ERRORS,
)
async def delete_comment(
    number: TaskNumber,
    comment_id: IdPath,
    session: SessionDep,
    user: CurrentUserDep,
    project: Delete,
) -> None:
    await comment_service.delete_comment(
        session, project.project, project.permissions, number, comment_id, user.id
    )
