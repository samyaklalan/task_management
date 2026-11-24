"""PRJ-1 to PRJ-6: create, list, open, update, archive and unarchive projects
(docs/api.md, section 11)."""

from typing import Annotated

from fastapi import APIRouter, Query
from fastapi import status as http_status

from app.api.deps import (
    CurrentUserDep,
    OrgPermissionsDep,
    ProjectContext,
    ProjectContextDep,
    SessionDep,
    require_org_permission,
    require_project_permission,
)
from app.core.permissions import Action, Feature
from app.models.project import ProjectStatus
from app.schemas.common import ErrorResponse, Page, SortOrder
from app.schemas.project import (
    CreateProjectRequest,  # name, task_prefix, description, start_date, end_date
    ProjectDetail,  # list item fields + description, created_at, task_counts, my_permissions
    ProjectListItem,  # public_id, name, status, task_prefix, start/end_date, updated_at
    ProjectSort,  # name / created_at / updated_at / end_date
    ProjectStatusFilter,  # active / on_hold / completed / archived
    UpdateProjectRequest,  # name, description, status, start/end_date, expected_updated_at
)
from app.services import project as project_service
from app.utils.pagination import PageQuery
from app.utils.search import SearchQuery

router = APIRouter(prefix="/projects", tags=["projects"])


# How the PRJ-2 filters flow (query parameters, not a JSON body):
#
#   GET /projects?status=on_hold&sort=name&order=asc
#           |
#           v
#   1. FastAPI checks it against ProjectStatusFilter =
#      Literal["active", "on_hold", "completed", "archived"]
#      "on_hold" -> ok      "paused" -> 422
#           |
#           v
#   2. Router converts the string -> ProjectStatus("on_hold")  (enum from models/project.py)
#           |
#           v
#   3. Service -> repository: WHERE projects.status = 'on_hold'
#
# Literal = the API contract (exact allowed values, shown in OpenAPI).
# Enum    = what the model/database use (Project.status).

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post(
    "",
    status_code=http_status.HTTP_201_CREATED,
    summary="Create a project with a General space and 3 default statuses (PRJ-1)",
    dependencies=[require_org_permission(Feature.PROJECTS, Action.CREATE)],
    responses={
        403: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "task_prefix already used"},
    },
)
async def create_project(
    body: CreateProjectRequest,
    session: SessionDep,
    user: CurrentUserDep,
    permissions: OrgPermissionsDep,
) -> ProjectDetail:
    return await project_service.create_project(
        session, user.organization_id, body, permissions
    )


# How create_project works (PRJ-1): router -> service -> repositories
# -------------------------------------------------------------------
# 1. Router (above): POST /projects, 201 Created on success.
#    Before it runs, FastAPI resolves the dependencies:
#    - CurrentUserDep (get_current_user): token -> user; organization_id comes from
#      the user's database row, never from the client.
#    - require_org_permission(PROJECTS, CREATE): the org-level role must allow
#      projects.create, otherwise 403.
#    - body: CreateProjectRequest (name, task_prefix, description?, start/end_date?),
#      otherwise 422.
#    - OrgPermissionsDep: the caller's permissions, used for my_permissions in the reply.
#    Errors are sent as {"error": {"code", "message", "details"}, "request_id"};
#    a duplicate task_prefix gives 409 with field "task_prefix" in details.
#
# 2. Service create_project (app/services/project.py), ONE transaction:
#    a) _insert_project: inserts the project with a random public_id.
#       for attempt in 1..3:
#           begin_nested()  -> a SAVEPOINT: a failed INSERT undoes only the savepoint,
#                              so the loop can retry in the same transaction
#           project_repository.create(...)  -> Project object, session.add, flush
#       except IntegrityError, by constraint name:
#           uq_projects_organization_id_task_prefix -> 409 ConflictError
#               (task_prefix must be unique within one organization)
#           uq_projects_public_id and attempts left -> continue with a NEW random id
#           anything else (or 3rd collision)        -> plain `raise` (original error, 500)
#       The AssertionError after the loop is unreachable: every attempt returns,
#       raises or continues.
#    b) space_repository.create(project_id, name="General", position=0):
#       the service ALWAYS passes these; every project starts with a General space (S-2).
#    c) task_status_repository.create_defaults(project.id): 3 statuses in ONE INSERT
#       (A-1), from DEFAULT_TASK_STATUSES (name, color, category):
#           To Do (0, todo) -> In Progress (1, in_progress) -> Done (2, done)
#       INSERT has no WHERE: project_id is a VALUE on each row.
#    d) session.commit(): project + space + 3 statuses saved together, or nothing.
#    e) _detail(project, {}, permissions): {} = no tasks yet, so all counts are 0.
#
# 3. Response: ProjectDetail with status 201.
#
# SQL it runs:
#    SAVEPOINT; INSERT INTO projects (...) VALUES (...) RETURNING id, ...; RELEASE SAVEPOINT;
#    INSERT INTO spaces (project_id, name, position) VALUES (:pid, 'General', 0);
#    INSERT INTO task_statuses (project_id, name, color, category, position)
#        VALUES (:pid, 'To Do', ..., 0), (:pid, 'In Progress', ..., 1), (:pid, 'Done', ..., 2);
#    COMMIT;


@router.get("", summary="Projects you can see (PRJ-2)")
async def list_projects(
    session: SessionDep,
    user: CurrentUserDep,
    permissions: OrgPermissionsDep,
    page: PageQuery,
    q: SearchQuery,
    status: Annotated[
        ProjectStatusFilter | None,
        Query(description="Default: everything except archived."),
    ] = None,
    sort: ProjectSort = "updated_at",
    order: SortOrder = "desc",
) -> Page[ProjectListItem]:
    return await project_service.list_projects(
        session,
        organization_id=user.organization_id,
        user_id=user.id,
        org_permissions=permissions,
        q=q,
        status=ProjectStatus(status) if status else None,
        sort=sort,
        order=order,
        page=page,
    )


# How list_projects works (PRJ-2), in simple words
# ------------------------------------------------
# Example: GET /projects?q=web&status=active&sort=name&order=asc&page=1&page_size=20
#
# What it receives:
#   - session: one database session for this request.
#   - user (get_current_user): the logged-in user; organization_id comes from their
#     database row, never from the client.
#   - permissions (OrgPermissionsDep): NOT a guard here (no 403). The service uses it
#     to decide WHICH projects you see:
#       org-level role that can see projects -> all projects of the organization
#       otherwise                             -> only projects where you have a role
#   - page (PageQuery): page number and page size (pagination).
#   - q (SearchQuery): text search, at least 3 characters.
#   - status (ProjectStatusFilter, a Literal): show only one status.
#       Not sent (None) -> everything EXCEPT archived.
#       Converted to the ProjectStatus enum before it goes to the service.
#   - sort (ProjectSort): column to sort by, default updated_at.
#   - order (SortOrder): asc / desc, default desc (newest changes first).
#   A value that isn't allowed (e.g. status=paused) -> 422.
#
# What it returns: Page[ProjectListItem]
#   Page[T] is generic: T can be ProjectListItem, a task item, ... Every page is
#     {"items": [...], "pagination": {"page", "page_size", "total_items", "total_pages"}}
#   Each item is built with ProjectListItem.model_validate(project):
#     the schema copies ONLY its own fields from the database object
#     (from_attributes=True), so the internal id, next_task_number, description, ...
#     are left out.
#   model_validate is NOT exclude_unset: exclude_unset is for update REQUESTS
#   ("change only the sent fields"); model_validate here builds a RESPONSE.


@router.get(
    "/{public_id}",
    summary="Project details, task counts and your permissions in it (PRJ-3)",
    responses={404: {"model": ErrorResponse}},
)
async def get_project(session: SessionDep, project: ProjectContextDep) -> ProjectDetail:
    return await project_service.get_project(
        session, project.project, project.permissions
    )


# How get_project works (PRJ-3), in easy words
# --------------------------------------------
# Example: GET /projects/AB12CD34   (public_id in the URL picks ONE project)
#
# 1. ProjectContextDep (get_project_context) runs FIRST:
#    - finds the project by (user.organization_id, public_id); organization_id comes
#      from the logged-in user's database row, never from the client
#    - load_permissions: the user's org-level role + their role in this project
#    - can_see_project: ANY role (org-level or in this project) is enough
#    - no project in your organization, or no role -> 404
#    The route gets the project AND the permissions from it; nothing is loaded twice.
#
# 2. project_service.get_project builds ProjectDetail:
#    - all project fields (public_id, name, status, description, dates, ...)
#    - my_permissions: for each project-level feature, for each action,
#      permissions.allows(feature, action) -> {"tasks": {"view": true, ...}, ...}
#      Each flag is COMPUTED by allows(); the frontend uses it to show/hide buttons.
#    - task_counts: ALL tasks of this project (not only yours), counted per status
#      CATEGORY (todo / in_progress / done), not per status name, + total.
#      e.g. a custom "Review" status with category in_progress counts as in_progress.
#    Only this endpoint has task_counts; the list (PRJ-2) leaves them out (speed).
#
# 3. Response: ProjectDetail, 200.
#
# "Can see the project" vs "view permission" (two different checks):
#   | Can see the project              | View permission                        |
#   | Are you PART of this project?    | What may you LOOK AT inside it?        |
#   | can_see_project (any role)       | allows(feature, VIEW) = role's flag    |
#   | one yes/no for the whole project | separate per feature (tasks, members.) |
#   | false -> 404 (seems not to exist) | false -> 403 (exists, not allowed)    |
#   Like a building: the key card gets you IN (404 without it); which ROOMS it opens
#   is the view permission (403).
#   This route checks ONLY "can see": no require_project_permission, so even a role
#   with no permissions can open the page (my_permissions then all false).
#   Routes inside the project (e.g. list tasks) add
#   require_project_permission(TASKS, VIEW) -> 403 without tasks.view.
#
#   Example, user with an empty role in project A:
#     GET /projects/A        -> 200, my_permissions all false
#     GET /projects/A/tasks  -> 403 (no tasks.view)
#   User with no role in project A:
#     GET /projects/A        -> 404


@router.patch(
    "/{public_id}",
    summary="Update name, description, status or dates (PRJ-4)",
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {
            "model": ErrorResponse,
            "description": "stale_update or project_archived",
        },
    },
)
async def update_project(
    body: UpdateProjectRequest,
    session: SessionDep,
    project: Annotated[
        ProjectContext, require_project_permission(Feature.PROJECTS, Action.EDIT)
    ],
) -> ProjectDetail:
    return await project_service.update_project(
        session, project.project, project.permissions, body
    )


# Both need projects.delete and both work on an archived project: archiving it
# again (or unarchiving an active one) is a harmless 200 with no change.
_ARCHIVE_GUARD = require_project_permission(
    Feature.PROJECTS, Action.DELETE, allowed_when_archived=True
)


@router.post(
    "/{public_id}/archive",
    summary="Archive: the project becomes read-only (PRJ-5)",
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def archive_project(
    session: SessionDep, project: Annotated[ProjectContext, _ARCHIVE_GUARD]
) -> ProjectDetail:
    return await project_service.set_archived(
        session, project.project, project.permissions, archived=True
    )


@router.post(
    "/{public_id}/unarchive",
    summary="Restore an archived project to active (PRJ-6)",
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def unarchive_project(
    session: SessionDep, project: Annotated[ProjectContext, _ARCHIVE_GUARD]
) -> ProjectDetail:
    return await project_service.set_archived(
        session, project.project, project.permissions, archived=False
    )
