"""USR-1 to USR-4: your profile and user administration (docs/api.md, section 8).

USR-5 to USR-7 (update, deactivate, org roles) follow in M5b.
"""

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import (
    CurrentUserDep,
    EmailSenderDep,
    OrgPermissionsDep,
    SessionDep,
    require_org_permission,
)

# to get current userbased on authorization
#  based on token or bearer tokem help yo finf the one who is logged in
# emailsenddep to send email when forget pass is there or rset link and
# orgnizationpermissiondepenede for see the role ois
#  organization levle to get the user who is habing organization levl role
# require_org_permissioms is used to see the feature is having with the org_levl one or not
# if feature is there then give depenecy else rais eerior
from app.core.permissions import Action, Feature
from app.schemas.common import ErrorResponse, Page, SortOrder

# for valid or structred valid repsonse we use the eror repsonse having message detail and code
# sort order SortOrder = Literal["asc", "desc"] for pagination
#
from app.schemas.user import (
    CreateUserRequest,
    MeResponse,
    UpdateMeRequest,
    UserOut,
    UserSort,
)

# Schema fields:
#   CreateUserRequest: name, email
#   MeResponse: id, name, email, organization, org_role, org_permissions
#   UpdateMeRequest: name
#   UserOut: id, name, email, is_active, org_role, created_at, updated_at
#   UserSort: "name", "email", "created_at"
# action we have crud as true or false and featurea ssorganization task project etc
from app.services import user as user_service
from app.utils.pagination import PageQuery
from app.utils.search import SearchQuery

# search query as to search user atleast 3 charcter need to be send
router = APIRouter(prefix="/users", tags=["users"])


# take session for intercating with db and summary and permisisons and return mereposne
@router.get("/me", summary="Who am I: profile, organization, org role and permissions (USR-1)")
async def get_me(
    session: SessionDep, user: CurrentUserDep, permissions: OrgPermissionsDep
) -> MeResponse:
    return await user_service.get_me(session, user, permissions)


# PATCH /users/me (USR-2): the logged-in user changes their own NAME and gets a
# MeResponse back (same as GET /me, with the new name).
#   1. UpdateMeRequest checks name: trimmed (spaces at start/end removed), 1 to max
#      length, optional. NOT lowercased: case is kept as typed ("SAMYAK" stays).
#   2. user_service.update_me -> user_repository.set_name (updates name and
#      updated_at), then commit.
#   3. Returns get_me(...) -> MeResponse with the new name.
# Sending {} (no name) changes nothing and returns the current MeResponse.
# Only email is lowercased (Email type); sorting the user list ignores case
# (func.lower) but never changes the stored name.
@router.patch("/me", summary="Change your own display name (USR-2)")
async def update_me(
    body: UpdateMeRequest,
    session: SessionDep,
    user: CurrentUserDep,
    permissions: OrgPermissionsDep,
) -> MeResponse:
    return await user_service.update_me(session, user, permissions, body)


@router.get(
    "",
    summary="Users of your organization (USR-3)",
    dependencies=[require_org_permission(Feature.USERS, Action.VIEW)],
    responses={403: {"model": ErrorResponse}},
)
async def list_users(
    session: SessionDep,
    user: CurrentUserDep,
    page: PageQuery,
    q: SearchQuery,
    is_active: Annotated[bool | None, Query(description="Only active or inactive users.")] = None,
    sort: UserSort = "name",
    order: SortOrder = "asc",
) -> Page[UserOut]:
    return await user_service.list_users(
        session,
        user.organization_id,
        q=q,
        is_active=is_active,
        sort=sort,
        order=order,
        page=page,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Add a user; they get an email to set their password (USR-4)",
    dependencies=[require_org_permission(Feature.USERS, Action.CREATE)],
    responses={
        403: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "Email already in use"},
    },
)
async def create_user(
    body: CreateUserRequest,
    session: SessionDep,
    user: CurrentUserDep,
    email_sender: EmailSenderDep,
) -> UserOut:
    return await user_service.create_user(session, user.organization_id, body, email_sender)
