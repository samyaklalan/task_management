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

# _bearer = HTTPBearer(auto_error=False) creates a reader for the token in the
# request header.
#   - HTTPBearer: reads the header "Authorization: Bearer eyJ..." and gives you
#     the token (eyJ...).
#   - auto_error=False: if the header is missing, it returns None instead of
#     raising FastAPI's own error. The code then raises the project's own 401,
#     so every error looks the same.
#   - _bearer: the _ means it's private to this file.
#   - It also adds the "Authorize" button in Swagger (the API docs page).
_bearer = HTTPBearer(auto_error=False)


# How get_current_user finds out who is logged in:
#   1. Read the token: _bearer takes it from "Authorization: Bearer <token>"
#      (the token from login, pasted into Authorize in Swagger).
#   2. No token -> 401.
#   3. Check the token: decode_access_token makes sure it is real (signed with our
#      secret), not expired (15 minutes) and an access token. Anything wrong -> 401.
#      If fine, it gives back user_id and organization_id.
#   4. Find the user in the database by user_id.
#   5. 401 if the user doesn't exist, is not active, or their organization doesn't
#      match the organization in the token.
#   6. All good -> return the user, and the route can use it.
# In one line: the token says who you are; the database confirms you're still allowed in.
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
    if user is None or not user.is_active or user.organization_id != claims.organization_id:
        raise NotAuthenticatedError()
    return user


# CurrentUserDep: short name for "run get_current_user and give me the User".
# Used by get_org_permissions below and by the routes (e.g. organization.py).
# FastAPI runs it once per request, so the token is checked only once.
CurrentUserDep = Annotated[User, Depends(get_current_user)]


# get_org_permissions: uses CurrentUserDep (the logged-in, authorized user) and
# load_permissions (app/services/authorization.py). load_permissions reads data
# already in the database (user_roles, roles, role_permissions) for this ONE user
# and works out their org-level role and what it allows. No project_id is passed,
# so only the org-level role is loaded (enough for org-scoped features like
# organization). allows() then uses the result to answer permission checks.
async def get_org_permissions(session: SessionDep, user: CurrentUserDep) -> Permissions:
    """The user's org-wide permissions. FastAPI runs a dependency once per
    request, so several checks in one request share this single query."""
    return await load_permissions(session, user.id)


OrgPermissionsDep = Annotated[Permissions, Depends(get_org_permissions)]
# load_permissions finds one user's roles
# and permissions from the database: their org-level role always,
#  and their project-level role only if a project_id is given.


def require_org_permission(feature: Feature, action: Action) -> params.Depends:
    """A route guard for organization-level features (403 if not allowed)."""

    async def check(permissions: OrgPermissionsDep) -> None:
        if not permissions.allows(feature, action):
            raise PermissionDeniedError()

    return Depends(check)


# OrgPermissionsDep is used by require_org_permission. Its check() calls
# permissions.allows(feature, action): if the user's org-level role doesn't allow
# that action on that feature, it raises PermissionDeniedError (403).
