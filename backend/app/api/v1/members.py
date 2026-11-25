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
    # can you see project does ur role allow meber.create
    # istt a change in archived projects
)
from app.core.permissions import Action, Feature
from app.schemas.common import ErrorResponse, IdPath, Page
from app.schemas.member import AddMemberRequest, ChangeMemberRoleRequest, MemberOut
from app.services import member as member_service
from app.utils.pagination import PageQuery
from app.utils.search import SearchQuery

# What get_project_context (app/api/deps.py) does, in short:
# 1. Takes public_id from the URL and the logged-in user.
# 2. Finds the project in YOUR organization -> not found = 404.
# 3. load_permissions loads your org-level role + your role in this project.
# 4. can_see_project: you need EITHER role -> neither = 404.
# 5. Returns ProjectContext(project, permissions) to the route.
# In one line: "Is this project in my organization, and do I have an org-level or
# project role? If yes, give me the project and my permissions; if not, 404."
# Every member route below uses it through require_project_permission /
# require_any_project_permission (which then add 403 / 409).

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
    # Do you belong to this project? (project in your org,
    # and you have an org-level role or a role in this project)
    # requirerole depe:Are you allowed this action? (members.view OR tasks.view)
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
