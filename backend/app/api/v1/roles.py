"""ROLE-1 to ROLE-4: the organization's roles and their permissions (docs/api.md, section 10)."""

from fastapi import APIRouter, status

from app.api.deps import (
    CurrentUserDep,
    OrgPermissionsDep,
    SessionDep,
    require_org_permission,
)
from app.core.permissions import Action, Feature
from app.schemas.common import ErrorResponse, IdPath
from app.schemas.role import (
    CreateRoleRequest,
    RoleListResponse,
    RoleOut,
    UpdateRoleRequest,
)
from app.services import role as role_service

router = APIRouter(prefix="/roles", tags=["roles"])


@router.get(
    "",
    summary="All roles with their permissions and how many users hold them (ROLE-1)",
    dependencies=[require_org_permission(Feature.ROLES, Action.VIEW)],
    responses={403: {"model": ErrorResponse}},
)
async def list_roles(session: SessionDep, user: CurrentUserDep) -> RoleListResponse:
    return await role_service.list_roles(session, user.organization_id)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a role with its permissions (ROLE-2)",
    dependencies=[require_org_permission(Feature.ROLES, Action.CREATE)],
    responses={403: {"model": ErrorResponse}},
)
async def create_role(
    body: CreateRoleRequest, session: SessionDep, user: CurrentUserDep
) -> RoleOut:
    return await role_service.create_role(session, user.organization_id, body)


@router.patch(
    "/{role_id}",
    summary="Rename a role and/or replace its permissions (ROLE-3)",
    dependencies=[require_org_permission(Feature.ROLES, Action.EDIT)],
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "stale_update or self_lockout"},
    },
)
async def update_role(
    role_id: IdPath,
    body: UpdateRoleRequest,
    session: SessionDep,
    user: CurrentUserDep,
    permissions: OrgPermissionsDep,
) -> RoleOut:
    return await role_service.update_role(session, user.organization_id, role_id, body, permissions)


@router.delete(
    "/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a role nobody holds (ROLE-4)",
    dependencies=[require_org_permission(Feature.ROLES, Action.DELETE)],
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "role_in_use"},
    },
)
async def delete_role(role_id: IdPath, session: SessionDep, user: CurrentUserDep) -> None:
    await role_service.delete_role(session, user.organization_id, role_id)
