"""Database access for roles, their permissions and role assignments.
Never commits (see app/repositories/user.py).

Tables:
- Role: id, organization_id, name. Each organization has its own roles.
- RolePermission: role_id, feature + 4 flags (view/create/edit/delete).
- UserRole: user_id, role_id, project_id. project_id NULL = org-wide role,
  a value = role on that project.

Functions:
- create: makes a role for an organization; flush() gives it an id without committing.
  Used at registration too.
- add_permissions: adds permission rows to a role that ALREADY exists (create first,
  otherwise a foreign-key error). No check/compare: it SETS role_id on each row,
  then one INSERT for all rows.
- assign_org_role: gives a user their org-wide role (project_id NULL); only one per user.
- list_with_details: ALL roles of the organization (the client sends no role name;
  organization_id comes from the logged-in user), each with its permissions and
  assigned_count = number of users holding it. Returns [(Role, count), ...].
- get_with_details: the same for ONE role; None if unknown or another organization's.
- update_if_unchanged: rename only if nobody saved in between (optimistic concurrency).
- replace_permissions: upsert all ten feature rows (ROLE-3).
- delete_role / count_assignments: delete a role; count who still holds it.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import Select, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Role, RolePermission, UserRole


async def create(session: AsyncSession, *, organization_id: int, name: str) -> Role:
    role = Role(organization_id=organization_id, name=name)
    session.add(role)
    await session.flush()
    return role


def _with_role_id(role_id: int, rows: list[dict]) -> list[dict]:
    """Copies of ``rows`` with ``role_id`` SET on each one (the caller's rows aren't changed)."""
    rows_with_role = []
    for row in rows:
        new_row = dict(row)  # copy, so the caller's row isn't changed
        new_row["role_id"] = role_id  # SET (not get): add the role this permission belongs to
        rows_with_role.append(new_row)
    return rows_with_role


async def add_permissions(session: AsyncSession, role_id: int, rows: list[dict]) -> None:
    """Insert permission rows (``{"feature": ..., "view_perm": ..., ...}``) in one statement."""
    # one INSERT for all rows
    await session.execute(insert(RolePermission), _with_role_id(role_id, rows))


async def assign_org_role(session: AsyncSession, *, user_id: int, role_id: int) -> None:
    """Give the user their organization-wide role (``project_id`` NULL).

    A second org-wide role for the same user fails on
    ``uq_user_roles_user_id_project_id`` (NULLS NOT DISTINCT).
    """
    session.add(UserRole(user_id=user_id, role_id=role_id, project_id=None))
    await session.flush()


def _with_details(organization_id: int) -> Select:
    """Shared base query for list_with_details and get_with_details.

    SQL (roughly):
        SELECT roles.*,
               (SELECT count(*) FROM user_roles
                WHERE user_roles.role_id = roles.id) AS assigned_count
        FROM roles
        WHERE roles.organization_id = :organization_id
    + a 2nd query for the permissions: ... WHERE role_id IN (...)
    """
    # scalar_subquery: the inner SELECT returns ONE value, used as a column.
    # correlate(Role): "roles" belongs to the OUTER query, so each role gets its
    # own count (not one total for every row). Counts org-wide AND project
    # assignments: the same rows that block delete_role.
    assigned_count = (
        select(func.count())
        .select_from(UserRole)
        .where(UserRole.role_id == Role.id)
        .correlate(Role)
        .scalar_subquery()
    )
    return (
        select(Role, assigned_count.label("assigned_count"))
        # only this organization's roles: another organization's role is never visible
        .where(Role.organization_id == organization_id)
        # Role.permissions is lazy="raise": load it with one extra query (IN), no N+1
        .options(selectinload(Role.permissions))
    )


async def list_with_details(session: AsyncSession, organization_id: int) -> list[tuple[Role, int]]:
    """ROLE-1: every role of the organization with its permissions and assigned_count.
    Not paginated: an organization has a handful of roles."""
    # lower(name): case-insensitive sort; id last so the order is always the same
    result = await session.execute(
        _with_details(organization_id).order_by(func.lower(Role.name), Role.id)
    )
    return [(role, count) for role, count in result.all()]


async def get_with_details(
    session: AsyncSession, organization_id: int, role_id: int
) -> tuple[Role, int] | None:
    """One role with its permissions and assigned_count; None if unknown or if it
    belongs to another organization (the service turns None into a 404).

    populate_existing: the role may already be in this session's cache with OLD
    permissions (the service loads it before replace_permissions changes the rows
    with bulk SQL). Without it, SQLAlchemy would keep the cached object and the
    response after an update would show the old permissions.
    """
    result = await session.execute(
        _with_details(organization_id)
        .where(Role.id == role_id)
        .execution_options(populate_existing=True)
    )
    row = result.one_or_none()
    if row is None:
        return None
    role, count = row
    return role, count


async def update_if_unchanged(
    session: AsyncSession,
    role_id: int,
    *,
    expected_updated_at: datetime,
    values: dict[str, Any],
) -> Role | None:
    """Optimistic update (api.md section 6): one UPDATE that only matches if nobody
    saved since the client loaded the role. ``None`` means "changed by someone else".

    ``values`` may be empty (only the permissions change): the UPDATE still runs, so
    ``updated_at`` moves on and the version check still happens. Bulk update() doesn't
    run onupdate, so updated_at is set here (rule 8).
    """
    return await session.scalar(
        update(Role)
        .where(Role.id == role_id, Role.updated_at == expected_updated_at)
        .values(**values, updated_at=func.statement_timestamp())
        .returning(Role)
        .execution_options(populate_existing=True)
    )


async def replace_permissions(session: AsyncSession, role_id: int, rows: list[dict]) -> None:
    """ROLE-3: write all ten feature rows in one statement.

    INSERT ... ON CONFLICT (role_id, feature) DO UPDATE: an existing row is updated
    in place, a missing one is inserted. Never delete + insert or replace
    ``role.permissions`` (that collides with uq_role_permissions_role_id_feature,
    see app/models/role.py).
    """
    stmt = pg_insert(RolePermission).values(_with_role_id(role_id, rows))
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[RolePermission.role_id, RolePermission.feature],
            # excluded = the row we tried to insert
            set_={
                "view_perm": stmt.excluded.view_perm,
                "create_perm": stmt.excluded.create_perm,
                "edit_perm": stmt.excluded.edit_perm,
                "delete_perm": stmt.excluded.delete_perm,
            },
        )
    )


async def delete_role(session: AsyncSession, role_id: int) -> None:
    """Delete the role; PostgreSQL removes its permission rows (ON DELETE CASCADE).

    Raises IntegrityError while anyone holds the role (fk_user_roles_role_id_roles,
    NO ACTION): the database decides, so a role assigned a moment ago is safe too.
    synchronize_session=False: don't update Python objects already in the session
    (nothing uses the deleted role afterwards).
    """
    await session.execute(
        delete(Role).where(Role.id == role_id).execution_options(synchronize_session=False)
    )


async def count_assignments(session: AsyncSession, role_id: int) -> int:
    """How many user_roles rows use this role (org-wide and project)."""
    count = await session.scalar(
        select(func.count()).select_from(UserRole).where(UserRole.role_id == role_id)
    )
    return count or 0
