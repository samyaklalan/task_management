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
from app.core.permissions import Action, Feature
from app.schemas.common import ErrorResponse, Page, SortOrder
from app.schemas.user import (
    CreateUserRequest,
    MeResponse,
    UpdateMeRequest,
    UserOut,
    UserSort,
)
from app.services import user as user_service
from app.utils.pagination import PageQuery
from app.utils.search import SearchQuery

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", summary="Who am I: profile, organization, org role and permissions (USR-1)")
async def get_me(
    session: SessionDep, user: CurrentUserDep, permissions: OrgPermissionsDep
) -> MeResponse:
    return await user_service.get_me(session, user, permissions)


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
