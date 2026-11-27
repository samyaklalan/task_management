"""Response shapes shared by every endpoint (docs/api.md, sections 3 and 4.1).

* ``ErrorResponse``: the body of every error. The handlers in
  app/core/exceptions.py build it; these classes describe it in the OpenAPI
  docs (``responses={404: {"model": ErrorResponse}}``) and let tests check that
  the handlers really produce this shape.
* ``Page[T]``: every paginated list, e.g. ``Page[TaskOut]``.
* ``SortOrder``: the ``order`` query parameter of every sortable list.
* ``RequestModel``: base class of every request body.
* ``RoleRef``: a role as other responses show it, ``{"id": 3, "name": "Manager"}``.
"""

from typing import Annotated, Any, Literal

from fastapi import Path
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

SortOrder = Literal["asc", "desc"]

# Ids are PostgreSQL BIGINT. A bigger number can't be an id, and sending it to
# the database would fail with an error (500) instead of a clean 404/422.
MAX_ID = 2**63 - 1
Id = Annotated[int, Field(ge=1, le=MAX_ID)]  # an id inside a request body
IdPath = Annotated[int, Path(ge=1, le=MAX_ID)]  # an id in the URL, e.g. /users/{user_id}

# Shared by organizations, projects, teams…: max 2000 characters, blank → null.
DESCRIPTION_MAX_LENGTH = 2000
Description = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=DESCRIPTION_MAX_LENGTH),
    AfterValidator(lambda text: text or None),
]


class RequestModel(BaseModel):
    """Base of every request body: an unknown field is a 422, not silently ignored,
    so a typo like ``{"nmae": "x"}`` is reported instead of doing nothing."""

    model_config = ConfigDict(extra="forbid")


# Yes. extra="forbid" means the client cannot send
#  extra fields. Any field the schema doesn't declare causes a 422 error.


class RoleRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


# RoleRef = {"id": 3, "name": "Manager"}


# It tells you which role a user has (id and name only), not what the role can do.
# It keeps API responses small and gives every role the same shape.
class UserRef(BaseModel):
    """A user as other items show them (member lists, assignees): id and name
    only. Emails are only listed by USR-3, for callers with ``users.view``."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


# ── Errors ────────────────────────────────────────────────────────────────────


class ErrorDetail(BaseModel):
    """One problem with one field, e.g. ``{"field": "title", "message": "..."}``."""

    field: str
    message: str


class ErrorBody(BaseModel):
    code: str = Field(description="Stable, machine-readable; the frontend switches on it.")
    message: str = Field(description="Human-readable; may change.")
    details: list[ErrorDetail] | dict[str, Any] | None = Field(
        default=None,
        description=(
            "Per-field problems (validation, conflict, invalid reference) or extra "
            "data for one error code, e.g. the current version for stale_update. "
            "Left out of the response when there is nothing to add."
        ),
    )


class ErrorResponse(BaseModel):
    error: ErrorBody
    request_id: str = Field(description="Matches the server log line for this request.")


# ── Pagination ────────────────────────────────────────────────────────────────


class Pagination(BaseModel):
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total_items: int = Field(ge=0, description="All matches, not just this page.")
    total_pages: int = Field(ge=0, description="0 when there are no matches.")

    @classmethod
    def build(cls, *, page: int, page_size: int, total_items: int) -> "Pagination":
        return cls(
            page=page,
            page_size=page_size,
            total_items=total_items,
            # Whole-number ceiling division: exact for any size (no float rounding).
            total_pages=-(-total_items // page_size),
        )


class Page[T](BaseModel):
    """A page of a list. ``items`` is empty when ``page`` is past the last page."""

    items: list[T]
    pagination: Pagination


# How a paginated list works, start to end (example: PRJ-2 list projects)
# ======================================================================
# Request: GET /projects?q=web&status=active&sort=name&order=asc&page=2&page_size=20
#
# 1. DEPENDENCIES (app/api/deps.py, app/utils/*), run by FastAPI BEFORE the route:
#    SessionDep        -> one AsyncSession for this request
#    CurrentUserDep    -> token -> user_id -> SELECT users WHERE id = ?        (query 1)
#                         401 if missing / invalid / inactive
#    OrgPermissionsDep -> load_permissions(user.id): org-level role + grants    (query 2)
#    PageQuery         -> page_params(): page >= 1 (default 1),
#                         1 <= page_size <= 100 (default 20), otherwise 422
#                         PageParams(page=2, page_size=20):
#                           offset = (page - 1) * page_size = 20, limit = 20
#    SearchQuery       -> q, at least 3 characters, otherwise 422
#    status / sort / order -> Literal checks (status=paused -> 422)
# es, with one detail: at request time it's the inner function that uses the default
# . The outer function only put the default there when the app started.
# page=2 (no page_size)	20, the default the outer function stored
# page=2&page_size=50	50, the client's value
# route using page_params(50), no page_size	50, that route's stored default
#    Code (app/utils/pagination.py):
#      PageQuery = Annotated[PageParams, Depends(page_params())]
#      def dependency(
#          page: Annotated[int, Query(ge=1)] = 1,
#          page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = default_page_size,
#      ) -> PageParams:
#          return PageParams(page=page, page_size=page_size)
#      # PageParams.offset = (page - 1) * page_size;  PageParams.limit = page_size
#
#    How PageQuery produces a PageParams object (dependency is a function, not a class):
#    - When the app starts: page_params() runs ONCE and RETURNS the inner function
#      dependency (return dependency, no brackets: returned, not called).
#      So Depends(page_params()) is really Depends(dependency).
#    - On each request (?page=2&page_size=20), FastAPI:
#        looks at dependency's parameters (page, page_size) and reads them from the
#        query string, checking ge=1 and le=100 (422 if they're wrong)
#        calls dependency(page=2, page_size=20)
#    - Inside dependency: PageParams(page=2, page_size=20) calls the class's
#      constructor. That's the line that creates the object.
#    - FastAPI passes that returned object into the route's `page` parameter.
#
#      app start:    page_params()  --returns-->  dependency   (a function)
#                                                     |
#      each request: FastAPI calls dependency(2, 20) --returns--> PageParams(2, 20)
#                                                     --> your route (as `page`)
#
#    In short:
#    - Outer (page_params()): runs ONCE, sets the DEFAULT page size (20, or 50 with
#      page_params(50)). It RETURNS the inner function, it doesn't call it.
#    - Inner (dependency): runs on EVERY request. FastAPI calls it with page and
#      page_size from the URL (or the defaults) and checks them
#      (page >= 1, page_size 1-100, otherwise 422).
#    - The inner function returns PageParams(page, page_size), which also gives
#      offset and limit.
#    - The route gets that object as `page` and passes it on to paginate, which runs
#      the count query and the LIMIT/OFFSET query.
#
# 2. ROUTER list_projects (app/api/v1/projects.py): thin, no logic.
#    Converts status "active" -> ProjectStatus.ACTIVE and calls the service.
#    Code:
#      @router.get("", summary="Projects you can see (PRJ-2)")
#      async def list_projects(
#          session: SessionDep, user: CurrentUserDep, permissions: OrgPermissionsDep,
#          page: PageQuery, q: SearchQuery,
#          status: Annotated[ProjectStatusFilter | None, Query(...)] = None,
#          sort: ProjectSort = "updated_at", order: SortOrder = "desc",
#      ) -> Page[ProjectListItem]:
#          return await project_service.list_projects(
#              session, organization_id=user.organization_id, user_id=user.id,
#              org_permissions=permissions, q=q,
#              status=ProjectStatus(status) if status else None,
#              sort=sort, order=order, page=page,
#          )
#
# 3. SERVICE list_projects (app/services/project.py):
#      sees_all True  -> the org-level role grants projects.view: every project of the org
#      sees_all False -> only projects where the user's PROJECT role grants projects.view
#    Code:
#      projects, pagination = await project_repository.list_visible(
#          session, organization_id=organization_id, user_id=user_id,
#          sees_all=Action.VIEW in org_permissions.org_grants[Feature.PROJECTS],
#          q=q, status=status, sort=sort, order=order, page=page,
#      )
#      (step 6 continues here)
#
# 4. REPOSITORY list_visible (app/repositories/project.py) builds ONE statement
#    (nothing runs until paginate):
#    Code:
#      statement = select(Project).where(Project.organization_id == organization_id)
#      if not sees_all:                     # only "my" projects, as a subquery
#          my_projects = (
#              select(UserRole.project_id)
#              .join(RolePermission, and_(
#                  RolePermission.role_id == UserRole.role_id,
#                  RolePermission.feature == "projects",
#                  RolePermission.view_perm.is_(True),
#              ))
#              .where(UserRole.user_id == user_id, UserRole.project_id.is_not(None))
#          )
#          statement = statement.where(Project.id.in_(my_projects))
#      if status is None:                   # default: hide archived
#          statement = statement.where(Project.status != ProjectStatus.ARCHIVED)
#      else:
#          statement = statement.where(Project.status == status)
#      if q is not None:                    # %, _ in q escaped by contains()
#          statement = statement.where(Project.name.ilike(contains(q), escape=LIKE_ESCAPE))
#      column = _SORT_COLUMNS[sort]         # "name" -> func.lower(Project.name)
#      if order == "desc":                  # id last: tie-breaker, stable pages
#          statement = statement.order_by(column.desc().nulls_last(), Project.id.desc())
#      else:
#          statement = statement.order_by(column.asc().nulls_last(), Project.id.asc())
#      return await paginate(session, statement, page)
#
# 5. paginate (app/utils/pagination.py): the SQL that runs
#    query 3, total (ORDER BY dropped, it doesn't change a count):
#      SELECT count(*) FROM (SELECT projects.* FROM projects WHERE <filters>) AS sub
#      -> total_items = 45
#    query 4, this page (SKIPPED if offset >= total_items, e.g. page=999):
#      SELECT projects.* FROM projects WHERE <filters>
#      ORDER BY lower(name) ASC NULLS LAST, id ASC LIMIT 20 OFFSET 20   -> rows 21-40
#    Code:
#      count_statement = select(func.count()).select_from(
#          statement.order_by(None).subquery()
#      )
#      total_items = await session.scalar(count_statement) or 0          # query 3
#      items = []
#      if params.offset < total_items:                                    # past the end?
#          page_statement = statement.limit(params.limit).offset(params.offset)
#          items = list(await session.scalars(page_statement))           # query 4
#      pagination = Pagination.build(
#          page=params.page, page_size=params.page_size, total_items=total_items
#      )
#      return items, pagination
#    Pagination.build (this file): total_pages = -(-total_items // page_size)
#      = ceil(45 / 20) = 3, whole-number math (no float rounding)
#
# 6. BACK IN THE SERVICE -> SCHEMAS (this file + app/schemas/project.py):
#    model_validate copies ONLY the schema's fields from each ORM object
#    (from_attributes=True): no internal id, no next_task_number, no description.
#    Code:
#      items = [ProjectListItem.model_validate(project) for project in projects]
#      return Page[ProjectListItem](items=items, pagination=pagination)
#
# 7. RESPONSE 200:
#    {"items": [{"public_id", "name", "status", "task_prefix", "start_date",
#                "end_date", "updated_at"}, ... 20],
#     "pagination": {"page": 2, "page_size": 20, "total_items": 45, "total_pages": 3}}
#
# Total: 4 queries for any page size, no N+1.
# Same 45 rows: page=1 -> 1-20, page=2 -> 21-40, page=3 -> 41-45, page=4 -> [] (no query 4)
# Other lists work the same: only the statement in step 4 and T in Page[T] change.


# My summary of the whole list + pagination flow (in my own words, corrected)
# ==========================================================================
# 1. Client calls GET /projects?...&page=2&page_size=20 with the token.
#    Dependencies run first: session, logged-in user (org id comes from the user's
#    row in the DB, never from the client), org permissions, page params, search, filters.
#
# 2. Page params: page_params() is the OUTER function, it runs ONCE at app start and
#    only sets the DEFAULT page size (20, or 50 with page_params(50)). It RETURNS the
#    inner function, it doesn't call it.
#    The INNER function (dependency) runs on EVERY request: FastAPI calls it with page
#    and page_size from the URL (if page_size isn't sent, it uses the default the outer
#    one stored), checks them (page >= 1, page_size 1-100, else 422) and it returns
#    PageParams(page, page_size). offset and limit are properties on that object:
#      offset = (page - 1) * page_size, limit = page_size
#    The route gets this object as `page` (it holds page AND page_size).
#
# 3. Router is thin: changes status text to the enum and calls the service.
#
# 4. Service decides sees_all = org-level role grants projects.view.
#
# 5. Repository builds ONE statement (nothing runs yet):
#    - first: only projects of my organization (organization_id = user's org id)
#    - `if not sees_all` runs when sees_all is FALSE: then only my_projects =
#      user_roles of the logged-in user with a project_id (project role), joined with
#      role_permissions on role_id, where feature = "projects" and view_perm is true.
#      So: projects where I'm assigned with a role that can VIEW projects.
#      If sees_all is TRUE (e.g. Admin) this is skipped: all projects of the org.
#    - status not sent -> hide archived (default view); sent -> only that status
#    - q -> name ILIKE '%q%' (% and _ escaped)
#    - ORDER BY the sort column asc/desc, then id (tie-breaker, stable pages)
#    - return await paginate(session, statement, page)
#
# 6. paginate (utils/pagination.py) is a PLAIN async function (not Pydantic;
#    only Pagination is a Pydantic model):
#    - query 1: SELECT count(*) FROM (statement without ORDER BY) AS sub -> total_items
#    - if offset >= total_items -> page is past the end, page query is SKIPPED, items []
#    - else query 2: the same statement (ORDER BY already in it) + LIMIT + OFFSET;
#      scalars() turns the rows into Project objects, stored in a list
#    - Pagination.build(page, page_size, total_items):
#      total_pages ROUNDS UP: -(-total_items // page_size)
#      e.g. 100 items, page size 30 -> 4 pages (30, 30, 30, 10), not 3
#    - returns (list of projects, pagination)
#
# 7. Back in the service: ProjectListItem.model_validate(project) copies ONLY the schema
#    fields from each project object (no internal id etc.), then
#    Page[ProjectListItem](items=items, pagination=pagination). Pydantic checks it and
#    FastAPI sends it as JSON: {"items": [...], "pagination": {...}}.
#
# 8. Pagination counts only the projects I'm ALLOWED to see (after all filters),
#    never the whole table.
