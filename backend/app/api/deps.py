"""Dependencies shared by the routers.

    async def endpoint(session: SessionDep): ...                     # database
    async def endpoint(user: CurrentUserDep): ...                    # logged in
    @router.get(..., dependencies=[require_org_permission(Feature.USERS, Action.VIEW)])

Project-scoped checks (a project's ``public_id`` in the URL, 404/403, archived)
are added with the projects module (M7).
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Path, params
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    NotAuthenticatedError,
    NotFoundError,
    PermissionDeniedError,
    ProjectArchivedError,
)
from app.core.permissions import Action, Feature
from app.core.security import decode_access_token
from app.db.session import get_session
from app.models import Project, User
from app.models.project import PROJECT_PUBLIC_ID_MAX_LENGTH, ProjectStatus
from app.repositories import project as project_repository
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


# ── Project-scoped routes ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProjectContext:
    """The project in the URL, and what the caller may do in it."""

    project: Project
    permissions: Permissions

    @property
    def is_archived(self) -> bool:
        return self.project.status == ProjectStatus.ARCHIVED


async def get_project_context(
    public_id: Annotated[str, Path(max_length=PROJECT_PUBLIC_ID_MAX_LENGTH)],
    session: SessionDep,
    user: CurrentUserDep,
) -> ProjectContext:
    """404 unless the project is in the caller's organization AND they have an
    org-wide role or a role in this project (A-5, rule 3). Another tenant's
    project, a made-up id and a project you can't access all look the same."""
    project = await project_repository.get_by_public_id(session, user.organization_id, public_id)
    # user.orgnization id is the user whioch is logged
    # in it uses cget_cuurnetnt_user dependency for that
    # you can see user is the currentuserdepen and which give
    # user orgniazation id only one who is logged in
    #
    if project is None:
        raise NotFoundError("Project")
    permissions = await load_permissions(session, user.id, project.id)
    if not permissions.can_see_project:
        raise NotFoundError("Project")
    return ProjectContext(project=project, permissions=permissions)


ProjectContextDep = Annotated[ProjectContext, Depends(get_project_context)]

# How get_project_context works (every route with a project in the URL)
# ---------------------------------------------------------------------
# Inputs: public_id from the URL (/projects/{public_id}/...), the request's session,
# and the logged-in user (from the token).
#
# 1. Find the project by (user.organization_id, public_id).
#    Not found -> 404. This IS the organization check: another organization's
#    project is never found.
# 2. load_permissions(session, user.id, project.id): the logged-in user's roles,
#    BOTH kinds, combined into one Permissions object:
#    - org-level role (user_roles.project_id IS NULL): always loaded
#    - project-level role (user_roles.project_id = this project): loaded because a
#      project_id is given
# 3. can_see_project: an org-level role OR a role in this project is enough.
#    Neither -> 404 (not 403), so a made-up id, another tenant's project and a
#    project you have no role in all look the same (A-5, rule 3).
# 4. Return ProjectContext(project, permissions): the route gets both without
#    loading them again.
#
# SEEING vs DOING: this dependency only decides "may you see this project" (404).
# What you may DO in it is checked afterwards by require_project_permission:
#   403 if the role doesn't allow the action, 409 project_archived for a write in
#   an archived project.
#
# Example: Ravi has org-level role Member (tasks: view) and Team Lead in project A
# (tasks: view, create, edit) -> in project A Ravi may view, create and edit tasks.


def require_project_permission(
    feature: Feature, action: Action, *, allowed_when_archived: bool | None = None
) -> params.Depends:
    """Guard for project-scoped routes; returns the ProjectContext.

    Order: 404 (can't see the project) → 403 (not allowed) → 409
    ``project_archived`` (a write in an archived project, A-9). Reads are
    allowed in archived projects; set ``allowed_when_archived=True`` for the
    few writes that must work there (unarchive).
    """
    if allowed_when_archived is None:
        allowed_when_archived = action is Action.VIEW
    return _project_guard(((feature, action),), allowed_when_archived=allowed_when_archived)


def require_any_project_permission(*grants: tuple[Feature, Action]) -> params.Depends:
    """Like require_project_permission for reads that one of several
    permissions allows, e.g. MEM-1: ``members.view`` OR ``tasks.view``."""
    return _project_guard(
        grants, allowed_when_archived=all(action is Action.VIEW for _, action in grants)
    )


def _project_guard(
    grants: tuple[tuple[Feature, Action], ...], *, allowed_when_archived: bool
) -> params.Depends:
    async def check(context: ProjectContextDep) -> ProjectContext:
        if not any(context.permissions.allows(feature, action) for feature, action in grants):
            raise PermissionDeniedError()
        if context.is_archived and not allowed_when_archived:
            raise ProjectArchivedError()
        return context

    return Depends(check)


# require_project_permission in simple words:
# "Before this route runs, make sure the user may do THIS action in THIS project."
#
# When it runs: FastAPI calls it automatically, BEFORE the route function, on every
# request to a route that lists it. The route code never calls it itself.
#
# What it checks, in order:
#   1. Can the user see the project?    (get_project_context)    no -> 404
#   2. Does their role allow the action? (permissions.allows)     no -> 403
#   3. Is it a write in an archived project?                      yes -> 409
#
# How a route uses it (not used by any route yet; for project/task/member routes):
#
#   @router.post(
#       "/projects/{public_id}/tasks",
#       dependencies=[require_project_permission(Feature.TASKS, Action.CREATE)],
#   )
#   async def create_task(...): ...
#
# Examples:
#   (TASKS, VIEW)    -> list tasks; works in archived projects too (reads are allowed)
#   (TASKS, CREATE)  -> create a task; blocked in archived projects (409)
#   (PROJECTS, EDIT, allowed_when_archived=True) -> unarchive; must work while archived
#
# Org version: require_org_permission (organization, users, roles). This one is for
# things inside a project.
