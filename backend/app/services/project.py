"""Projects (docs/api.md, PRJ-1 to PRJ-6)."""

import secrets
import string

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ConflictError,
    FieldValidationError,
    NotFoundError,
    ProjectArchivedError,
    StaleUpdateError,
)
from app.core.permissions import Action, Feature
from app.db.errors import constraint_name
from app.models import Project
from app.models.project import ProjectStatus
from app.models.space import DEFAULT_SPACE_NAME
from app.repositories import project as project_repository
from app.repositories import space as space_repository
from app.repositories import task_status as task_status_repository
from app.schemas.common import Page, SortOrder
from app.schemas.project import (
    DATE_RANGE_MESSAGE,
    CreateProjectRequest,
    ProjectDetail,
    ProjectListItem,
    ProjectSort,
    TaskCounts,
    UpdateProjectRequest,
)
from app.schemas.user import ActionFlags
from app.services.authorization import Permissions
from app.utils.pagination import PageParams

# The project-level features shown in my_permissions (PRJ-3).
_PROJECT_FEATURES = (
    Feature.PROJECTS,
    Feature.MEMBERS,
    Feature.TEAMS,
    Feature.SPACES,
    Feature.TASK_STATUSES,
    Feature.TASKS,
    Feature.COMMENTS,
)

# public_id: 12 random lowercase letters and digits (about 62 bits), e.g.
# "k3x9p2m7qa1z". Not guessable, so a project URL reveals nothing.
_PUBLIC_ID_ALPHABET = string.ascii_lowercase + string.digits
_PUBLIC_ID_LENGTH = 12
_PUBLIC_ID_ATTEMPTS = 3


def new_public_id() -> str:
    return "".join(secrets.choice(_PUBLIC_ID_ALPHABET) for _ in range(_PUBLIC_ID_LENGTH))


# ── PRJ-1 create ──────────────────────────────────────────────────────────────


async def create_project(
    session: AsyncSession,
    organization_id: int,
    data: CreateProjectRequest,
    caller_permissions: Permissions,
) -> ProjectDetail:
    """The project, its "General" space (S-2) and its three default statuses
    (A-1), in one transaction."""
    project = await _insert_project(session, organization_id, data)
    await space_repository.create(
        session, project_id=project.id, name=DEFAULT_SPACE_NAME, position=0
    )
    await task_status_repository.create_defaults(session, project.id)
    await session.commit()
    return _detail(project, {}, caller_permissions)


async def _insert_project(
    session: AsyncSession, organization_id: int, data: CreateProjectRequest
) -> Project:
    for attempt in range(1, _PUBLIC_ID_ATTEMPTS + 1):
        try:
            # A savepoint, so a failed insert can be retried in the same transaction.
            async with session.begin_nested():
                return await project_repository.create(
                    session,
                    organization_id=organization_id,
                    public_id=new_public_id(),
                    name=data.name,
                    task_prefix=data.task_prefix,
                    description=data.description,
                    start_date=data.start_date,
                    end_date=data.end_date,
                )
        except IntegrityError as exc:
            name = constraint_name(exc)
            if name == "uq_projects_organization_id_task_prefix":
                raise ConflictError(
                    "This task prefix is already used in your organization.",
                    field="task_prefix",
                ) from exc
            if name == "uq_projects_public_id" and attempt < _PUBLIC_ID_ATTEMPTS:
                continue  # astronomically unlikely: just pick another id
            raise
    raise AssertionError("unreachable")  # pragma: no cover


# ── PRJ-2 list ────────────────────────────────────────────────────────────────


async def list_projects(
    session: AsyncSession,
    *,
    organization_id: int,
    user_id: int,
    org_permissions: Permissions,
    q: str | None,
    status: ProjectStatus | None,
    sort: ProjectSort,
    order: SortOrder,
    page: PageParams,
) -> Page[ProjectListItem]:
    projects, pagination = await project_repository.list_visible(
        session,
        organization_id=organization_id,
        user_id=user_id,
        sees_all=Action.VIEW in org_permissions.org_grants[Feature.PROJECTS],
        q=q,
        status=status,
        sort=sort,
        order=order,
        page=page,
    )
    items = [ProjectListItem.model_validate(project) for project in projects]
    return Page[ProjectListItem](items=items, pagination=pagination)


# ── PRJ-3 details ─────────────────────────────────────────────────────────────


async def get_project(
    session: AsyncSession, project: Project, permissions: Permissions
) -> ProjectDetail:
    # Without projects.view the project doesn't exist for the caller (PRJ-3
    # lists only 404, and PRJ-2 wouldn't show it either).
    if not permissions.allows(Feature.PROJECTS, Action.VIEW):
        raise NotFoundError("Project")
    return await _with_counts(session, project, permissions)


# ── PRJ-4 update ──────────────────────────────────────────────────────────────


async def update_project(
    session: AsyncSession,
    project: Project,
    permissions: Permissions,
    data: UpdateProjectRequest,
) -> ProjectDetail:
    """Change name, description, status and/or dates, only if nobody saved in
    between (409 ``stale_update`` with the current project in ``details.current``).

    The route's guard already refused archived projects (409 ``project_archived``).
    """
    # Only the fields the client actually sent ("end_date": null clears it).
    values = data.model_dump(include=data.model_fields_set - {"expected_updated_at"})

    if project.updated_at != data.expected_updated_at:
        # Already stale when we loaded it: no need to try the UPDATE.
        raise StaleUpdateError(current=await _current(session, project, permissions))

    # The schema checks the dates when both are sent; a single date has to be
    # checked against the stored other one. ``project`` is exactly the version
    # the caller expects (checked just above), so this check is valid.
    start = values.get("start_date", project.start_date)
    end = values.get("end_date", project.end_date)
    if start is not None and end is not None and end < start:
        raise FieldValidationError("end_date", DATE_RANGE_MESSAGE)

    if values:
        project_id = project.id  # read before a possible rollback expires `project`
        updated = await project_repository.update_if_unchanged(
            session,
            project_id,
            expected_updated_at=data.expected_updated_at,
            values=values,
        )
        if updated is None:
            # Someone saved or archived it between our SELECT and our UPDATE.
            await session.rollback()
            current = await _reload(session, project_id)
            if current.status == ProjectStatus.ARCHIVED:
                raise ProjectArchivedError()
            raise StaleUpdateError(current=await _current(session, current, permissions))
        project = updated
        await session.commit()

    return await _with_counts(session, project, permissions)


# ── PRJ-5 archive / PRJ-6 unarchive ───────────────────────────────────────────


async def set_archived(
    session: AsyncSession, project: Project, permissions: Permissions, *, archived: bool
) -> ProjectDetail:
    """Archive (read-only, A-9) or restore to ``active``. Doing it twice is a
    harmless 200 with no change (the repeated click, or two people at once)."""
    project_id = project.id
    # Wait for writes already running in this project (tasks, teams…), so none
    # of them lands after the archive (A-9, see lock_for_child_write).
    await project_repository.lock_for_archive(session, project_id)
    changed = await project_repository.set_archived(session, project_id, archived=archived)
    await session.commit()
    # Unchanged: another request may have just done it, so show the stored row.
    project = changed or await _reload(session, project_id)
    return await _with_counts(session, project, permissions)


# ── Shared ────────────────────────────────────────────────────────────────────


async def _reload(session: AsyncSession, project_id: int) -> Project:
    project = await session.get(Project, project_id, populate_existing=True)
    if project is None:  # projects are never deleted through the API (P-4)
        raise NotFoundError("Project")
    return project


async def _current(session: AsyncSession, project: Project, permissions: Permissions) -> dict:
    """The project as PRJ-3 shows it, for ``details.current`` of a 409."""
    return (await _with_counts(session, project, permissions)).model_dump(mode="json")


async def _with_counts(
    session: AsyncSession, project: Project, permissions: Permissions
) -> ProjectDetail:
    counts = await project_repository.task_counts(session, project.id)
    return _detail(project, counts, permissions)


def _detail(project: Project, counts: dict[str, int], permissions: Permissions) -> ProjectDetail:
    return ProjectDetail(
        public_id=project.public_id,
        name=project.name,
        status=project.status,
        description=project.description,
        task_prefix=project.task_prefix,
        start_date=project.start_date,
        end_date=project.end_date,
        created_at=project.created_at,
        updated_at=project.updated_at,
        task_counts=TaskCounts(**counts, total=sum(counts.values())),
        my_permissions={
            feature.value: ActionFlags(
                **{action.value: permissions.allows(feature, action) for action in Action}
            )
            for feature in _PROJECT_FEATURES
        },
    )
