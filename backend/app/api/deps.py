"""Dependencies shared by the routers.

    async def endpoint(session: SessionDep): ...                     # database
    async def endpoint(user: CurrentUserDep): ...                    # logged in
    @router.get(..., dependencies=[require_org_permission(Feature.USERS, Action.VIEW)])

Project-scoped checks (a project's ``public_id`` in the URL, 404/403, archived)
are added with the projects module (M7).
"""

from typing import Annotated

from fastapi import Depends, params
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotAuthenticatedError, PermissionDeniedError
from app.core.permissions import Action, Feature
from app.core.security import decode_access_token
from app.db.session import get_session
from app.models import User
from app.repositories import user as user_repository
from app.services.authorization import Permissions, load_permissions
from app.services.email import EmailSender, get_email_sender

# One database session per request (app/db/session.py); the service commits.
SessionDep = Annotated[AsyncSession, Depends(get_session)]

# How emails are sent (console in development); tests swap in a recorder.
EmailSenderDep = Annotated[EmailSender, Depends(get_email_sender)]

# Reads "Authorization: Bearer <token>". auto_error=False: a missing header is
# answered with our own 401 error body, not FastAPI's. It also adds the
# "Authorize" button to the API docs page.
_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """The logged-in user (section 2.1). Loaded from the database on every
    request, so a deactivated user is locked out at once, not after 15 minutes."""
    if credentials is None:
        raise NotAuthenticatedError()
    claims = decode_access_token(credentials.credentials)  # 401 if invalid or expired
    user = await user_repository.get_by_id(session, claims.user_id)
    if (
        user is None
        or not user.is_active
        or user.organization_id != claims.organization_id
    ):
        raise NotAuthenticatedError()
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


async def get_org_permissions(session: SessionDep, user: CurrentUserDep) -> Permissions:
    """The user's org-wide permissions. FastAPI runs a dependency once per
    request, so several checks in one request share this single query."""
    return await load_permissions(session, user.id)


OrgPermissionsDep = Annotated[Permissions, Depends(get_org_permissions)]


def require_org_permission(feature: Feature, action: Action) -> params.Depends:
    """A route guard for organization-level features (403 if not allowed)."""

    async def check(permissions: OrgPermissionsDep) -> None:
        if not permissions.allows(feature, action):
            raise PermissionDeniedError()

    return Depends(check)
