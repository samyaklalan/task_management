"""Database access for projects. Never commits (see app/repositories/user.py)."""

from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, RolePermission, Task, TaskStatus, UserRole
from app.models.project import ProjectStatus
from app.schemas.common import Pagination, SortOrder
from app.schemas.project import ProjectSort
from app.utils.pagination import PageParams, paginate
from app.utils.search import LIKE_ESCAPE, contains


async def create(
    session: AsyncSession,
    *,
    organization_id: int,
    public_id: str,
    name: str,
    task_prefix: str,
    description: str | None,
    start_date: Any,
    end_date: Any,
) -> Project:
    project = Project(
        organization_id=organization_id,
        public_id=public_id,
        name=name,
        task_prefix=task_prefix,
        description=description,
        start_date=start_date,
        end_date=end_date,
    )
    session.add(project)
    # IntegrityError: uq_projects_organization_id_task_prefix or uq_projects_public_id
    await session.flush()
    return project


# used by the dependecy too too see public id belong to its own orgniazation or
# not as we are only allowed to see project belong to that org where user also belong
# user belong to certain orgiazatiomn and organization has projevy
# so if user is having org or p[roject level role then it is allowed to too see project
# nooneelse is allowed
async def get_by_public_id(
    session: AsyncSession, organization_id: int, public_id: str
) -> Project | None:
    """Only within the caller's organization: another tenant's project is "not found"."""
    return await session.scalar(
        select(Project).where(
            Project.public_id == public_id, Project.organization_id == organization_id
        )
    )


# org id is coming from user who is logged in and
# its uses get_cuurent_user dependency


# SQL get_by_public_id runs:
#   SELECT * FROM projects
#   WHERE projects.public_id = :public_id
#     AND projects.organization_id = :organization_id;
# Both conditions in ONE query: another organization's project simply isn't found
# (None -> 404), so the organization check needs no extra query.
async def update_if_unchanged(
    session: AsyncSession,
    project_id: int,
    *,
    expected_updated_at: datetime,
    values: dict[str, Any],
) -> Project | None:
    """PRJ-4 optimistic update: one UPDATE that only matches if nobody saved
    since the caller loaded the project AND it isn't archived. ``None`` means
    one of the two changed in between; the service finds out which."""
    return await session.scalar(
        update(Project)
        .where(
            Project.id == project_id,
            Project.updated_at == expected_updated_at,
            Project.status != ProjectStatus.ARCHIVED,
        )
        .values(**values, updated_at=func.statement_timestamp())
        .returning(Project)
        .execution_options(populate_existing=True)
    )


async def set_archived(
    session: AsyncSession, project_id: int, *, archived: bool
) -> Project | None:
    """PRJ-5 (``archived=True``) and PRJ-6 (back to ``active``).

    Only changes a project that isn't already in the target state; ``None``
    means "nothing to do", so a repeated click doesn't touch ``updated_at``.
    """
    is_archived = Project.status == ProjectStatus.ARCHIVED
    return await session.scalar(
        update(Project)
        .where(Project.id == project_id, ~is_archived if archived else is_archived)
        .values(
            status=ProjectStatus.ARCHIVED if archived else ProjectStatus.ACTIVE,
            updated_at=func.statement_timestamp(),
        )
        .returning(Project)
        .execution_options(populate_existing=True)
    )


_SORT_COLUMNS = {
    "name": func.lower(Project.name),
    "created_at": Project.created_at,
    "updated_at": Project.updated_at,
    "end_date": Project.end_date,
}


async def list_visible(
    session: AsyncSession,
    *,
    organization_id: int,
    user_id: int,
    sees_all: bool,
    q: str | None,
    status: ProjectStatus | None,
    sort: ProjectSort,
    order: SortOrder,
    page: PageParams,
) -> tuple[list[Project], Pagination]:
    """PRJ-2: the projects the user may see, filtered before paging.

    ``sees_all``: the user's org-wide role grants ``projects.view``. Otherwise
    only projects where their project role grants it (a subquery, so the whole
    list is still one query plus the count).
    """
    statement: Select[Any] = select(Project).where(
        Project.organization_id == organization_id
    )

    if not sees_all:
        my_projects = (
            select(UserRole.project_id)
            .join(
                RolePermission,
                and_(
                    RolePermission.role_id == UserRole.role_id,
                    RolePermission.feature == "projects",
                    RolePermission.view_perm.is_(True),
                ),
            )
            .where(UserRole.user_id == user_id, UserRole.project_id.is_not(None))
        )
        statement = statement.where(Project.id.in_(my_projects))

    if status is None:
        statement = statement.where(Project.status != ProjectStatus.ARCHIVED)
    else:
        statement = statement.where(Project.status == status)
    if q is not None:
        statement = statement.where(Project.name.ilike(contains(q), escape=LIKE_ESCAPE))

    column = _SORT_COLUMNS[sort]
    if order == "desc":
        statement = statement.order_by(column.desc().nulls_last(), Project.id.desc())
    else:
        statement = statement.order_by(column.asc().nulls_last(), Project.id.asc())

    return await paginate(session, statement, page)


async def task_counts(session: AsyncSession, project_id: int) -> dict[str, int]:
    """Tasks per status category, e.g. ``{"todo": 12, "done": 30}``: one GROUP BY."""
    rows = await session.execute(
        select(TaskStatus.category, func.count(Task.id))
        .join(Task, Task.status_id == TaskStatus.id)
        .where(Task.project_id == project_id)
        .group_by(TaskStatus.category)
    )
    return {category: count for category, count in rows}
