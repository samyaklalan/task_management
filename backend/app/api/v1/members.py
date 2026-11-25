"""MEM-1 to MEM-4: who can access a project, and their project roles
(docs/api.md, section 11)."""

from typing import Annotated

from fastapi import APIRouter
from fastapi import status as http_status

from app.api.deps import (
    ProjectContext,
    SessionDep,
    require_any_project_permission,
    require_project_permission,
)
from app.core.permissions import Action, Feature
from app.schemas.common import ErrorResponse, IdPath, Page
from app.schemas.member import AddMemberRequest, ChangeMemberRoleRequest, MemberOut
from app.services import member as member_service
from app.utils.pagination import PageQuery
from app.utils.search import SearchQuery

router = APIRouter(prefix="/projects/{public_id}/members", tags=["members"])

_ERRORS = {403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}


@router.get("", summary="Who can access this project; also the assignee picker (MEM-1)")
async def list_members(
    session: SessionDep,
    # Task editors need the picker without member-management rights.
    project: Annotated[
        ProjectContext,
        require_any_project_permission(
            (Feature.MEMBERS, Action.VIEW), (Feature.TASKS, Action.VIEW)
        ),
    ],
    page: PageQuery,
    q: SearchQuery,
) -> Page[MemberOut]:
    return await member_service.list_members(session, project.project, q=q, page=page)


@router.post(
    "",
    status_code=http_status.HTTP_201_CREATED,
    summary="Give an organization user a role in this project (MEM-2)",
    responses=_ERRORS
    | {
        409: {
            "model": ErrorResponse,
            "description": "already_member or project_archived",
        }
    },
)
async def add_member(
    body: AddMemberRequest,
    session: SessionDep,
    project: Annotated[ProjectContext, require_project_permission(Feature.MEMBERS, Action.CREATE)],
) -> MemberOut:
    return await member_service.add_member(session, project.project, body)


@router.patch(
    "/{user_id}",
    summary="Change a member's project role (MEM-3)",
    responses=_ERRORS | {409: {"model": ErrorResponse, "description": "project_archived"}},
)
async def change_member_role(
    user_id: IdPath,
    body: ChangeMemberRoleRequest,
    session: SessionDep,
    project: Annotated[ProjectContext, require_project_permission(Feature.MEMBERS, Action.EDIT)],
) -> MemberOut:
    return await member_service.change_role(session, project.project, user_id, body)


@router.delete(
    "/{user_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    summary="Remove a member; unassigns their tasks and team memberships here (MEM-4)",
    responses=_ERRORS | {409: {"model": ErrorResponse, "description": "project_archived"}},
)
async def remove_member(
    user_id: IdPath,
    session: SessionDep,
    project: Annotated[ProjectContext, require_project_permission(Feature.MEMBERS, Action.DELETE)],
) -> None:
    await member_service.remove_member(session, project.project, user_id)
