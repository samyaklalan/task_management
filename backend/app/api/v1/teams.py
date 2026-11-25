"""TEAM-1 to TEAM-8: teams of a project and their members (docs/api.md, section 12)."""

from typing import Annotated

from fastapi import APIRouter
from fastapi import status as http_status

from app.api.deps import ProjectContext, SessionDep, require_project_permission
from app.core.permissions import Action, Feature
from app.schemas.common import ErrorResponse, IdPath, Page
from app.schemas.team import (
    AddTeamMembersRequest,
    AddTeamMembersResponse,
    CreateTeamRequest,
    TeamMemberOut,
    TeamMemberSort,
    TeamOut,
    TeamSort,
    UpdateTeamRequest,
)
from app.services import team as team_service
from app.utils.pagination import PageQuery
from app.utils.search import SearchQuery

router = APIRouter(prefix="/projects/{public_id}/teams", tags=["teams"])

View = Annotated[ProjectContext, require_project_permission(Feature.TEAMS, Action.VIEW)]
Create = Annotated[ProjectContext, require_project_permission(Feature.TEAMS, Action.CREATE)]
Edit = Annotated[ProjectContext, require_project_permission(Feature.TEAMS, Action.EDIT)]
Delete = Annotated[ProjectContext, require_project_permission(Feature.TEAMS, Action.DELETE)]

_READ_ERRORS = {403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}
_WRITE_ERRORS = _READ_ERRORS | {
    409: {"model": ErrorResponse, "description": "project_archived"},
}


@router.get("", summary="Teams with member counts (TEAM-1)", responses=_READ_ERRORS)
async def list_teams(
    session: SessionDep,
    project: View,
    page: PageQuery,
    q: SearchQuery,
    sort: TeamSort = "name",
) -> Page[TeamOut]:
    return await team_service.list_teams(session, project.project, q=q, sort=sort, page=page)


@router.post(
    "",
    status_code=http_status.HTTP_201_CREATED,
    summary="Create a team (TEAM-2)",
    responses=_WRITE_ERRORS,
)
async def create_team(body: CreateTeamRequest, session: SessionDep, project: Create) -> TeamOut:
    return await team_service.create_team(session, project.project, body)


@router.get("/{team_id}", summary="One team (TEAM-3)", responses=_READ_ERRORS)
async def get_team(team_id: IdPath, session: SessionDep, project: View) -> TeamOut:
    return await team_service.get_team(session, project.project, team_id)


@router.patch(
    "/{team_id}",
    summary="Rename or describe a team (TEAM-4)",
    responses=_READ_ERRORS
    | {409: {"model": ErrorResponse, "description": "stale_update or project_archived"}},
)
async def update_team(
    team_id: IdPath, body: UpdateTeamRequest, session: SessionDep, project: Edit
) -> TeamOut:
    return await team_service.update_team(session, project.project, team_id, body)


@router.delete(
    "/{team_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    summary="Delete a team; its memberships go with it (TEAM-5)",
    responses=_WRITE_ERRORS,
)
async def delete_team(team_id: IdPath, session: SessionDep, project: Delete) -> None:
    await team_service.delete_team(session, project.project, team_id)


@router.get("/{team_id}/members", summary="Members of a team (TEAM-6)", responses=_READ_ERRORS)
async def list_team_members(
    team_id: IdPath,
    session: SessionDep,
    project: View,
    page: PageQuery,
    sort: TeamMemberSort = "name",
) -> Page[TeamMemberOut]:
    return await team_service.list_members(session, project.project, team_id, sort=sort, page=page)


@router.post(
    "/{team_id}/members",
    summary="Add up to 50 users; ones already in the team are skipped (TEAM-7)",
    responses=_WRITE_ERRORS,
)
async def add_team_members(
    team_id: IdPath, body: AddTeamMembersRequest, session: SessionDep, project: Edit
) -> AddTeamMembersResponse:
    return await team_service.add_members(session, project.project, team_id, body)


@router.delete(
    "/{team_id}/members/{user_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    summary="Remove a user from the team (TEAM-8)",
    responses=_WRITE_ERRORS,
)
async def remove_team_member(
    team_id: IdPath, user_id: IdPath, session: SessionDep, project: Edit
) -> None:
    await team_service.remove_member(session, project.project, team_id, user_id)
