"""Database access for roles, their permissions and role assignments.
Never commits (see app/repositories/user.py)."""

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Role, RolePermission, UserRole


async def create(session: AsyncSession, *, organization_id: int, name: str) -> Role:
    role = Role(organization_id=organization_id, name=name)
    session.add(role)
    await session.flush()
    return role


async def add_permissions(session: AsyncSession, role_id: int, rows: list[dict]) -> None:
    """Insert permission rows (``{"feature": ..., "view_perm": ..., ...}``) in one statement."""
    await session.execute(insert(RolePermission), [row | {"role_id": role_id} for row in rows])


async def assign_org_role(session: AsyncSession, *, user_id: int, role_id: int) -> None:
    """Give the user their organization-wide role (``project_id`` NULL).

    A second org-wide role for the same user fails on
    ``uq_user_roles_user_id_project_id`` (NULLS NOT DISTINCT).
    """
    session.add(UserRole(user_id=user_id, role_id=role_id, project_id=None))
    await session.flush()


async def get_in_organization(
    session: AsyncSession, organization_id: int, role_id: int
) -> Role | None:
    """A role, but only if it belongs to this organization."""
    return await session.scalar(
        select(Role).where(Role.id == role_id, Role.organization_id == organization_id)
    )


async def get_org_role(session: AsyncSession, user_id: int) -> Role | None:
    """The user's organization-wide role, if they have one."""
    return await session.scalar(
        select(Role)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id, UserRole.project_id.is_(None))
    )


async def set_org_role(session: AsyncSession, *, user_id: int, role_id: int) -> None:
    """Give the user this org-wide role, replacing any existing one (USR-6).

    One atomic statement: ``INSERT ... ON CONFLICT ... DO UPDATE``. It relies on
    the NULLS NOT DISTINCT unique constraint, so two simultaneous requests can
    never leave the user with two org-wide roles.
    """
    statement = pg_insert(UserRole).values(user_id=user_id, role_id=role_id, project_id=None)
    await session.execute(
        statement.on_conflict_do_update(
            constraint="uq_user_roles_user_id_project_id",
            set_={"role_id": statement.excluded.role_id},
        )
    )


async def remove_org_role(session: AsyncSession, user_id: int) -> None:
    """Remove the user's org-wide role (USR-7); their project roles stay."""
    await session.execute(
        delete(UserRole).where(UserRole.user_id == user_id, UserRole.project_id.is_(None))
    )


# ── Roles with their permissions (ROLE-1 to ROLE-4) ───────────────────────────

# How many users hold the role (org-wide or in any project).
_assigned_count = (
    select(func.count())
    .where(UserRole.role_id == Role.id)
    .correlate(Role)
    .scalar_subquery()
    .label("assigned_count")
)


async def list_with_details(session: AsyncSession, organization_id: int) -> list[tuple[Role, int]]:
    """All roles of the organization, each with its permission rows and how
    many users hold it. Two queries in total, however many roles there are
    (roles + counts, then all their permission rows)."""
    rows = await session.execute(
        select(Role, _assigned_count)
        .where(Role.organization_id == organization_id)
        .options(selectinload(Role.permissions))
        .order_by(func.lower(Role.name), Role.id)
    )
    return [(role, count) for role, count in rows]


async def get_with_details(
    session: AsyncSession, organization_id: int, role_id: int
) -> tuple[Role, int] | None:
    """One role of the organization with fresh permission rows and its count."""
    row = (
        await session.execute(
            select(Role, _assigned_count)
            .where(Role.id == role_id, Role.organization_id == organization_id)
            .options(selectinload(Role.permissions))
            .execution_options(populate_existing=True)  # re-read after a change
        )
    ).one_or_none()
    return (row[0], row[1]) if row else None


async def update_if_unchanged(
    session: AsyncSession, role_id: int, *, expected_updated_at: datetime, values: dict[str, Any]
) -> bool:
    """Optimistic update of the role row. ``updated_at`` always moves on, also
    when only the permissions change, so the next editor sees a new version.
    The row stays locked until commit, so two edits of one role run one after
    the other. False: someone else saved first."""
    result = await session.execute(
        update(Role)
        .where(Role.id == role_id, Role.updated_at == expected_updated_at)
        .values(**values, updated_at=func.statement_timestamp())
    )
    return result.rowcount == 1


async def replace_permissions(session: AsyncSession, role_id: int, rows: list[dict]) -> None:
    """Set the given features' switches, inserting missing rows (ROLE-3).

    An upsert (``ON CONFLICT ... DO UPDATE``), not "delete all, insert again":
    PostgreSQL checks the INSERT before the DELETE takes effect, so a plain
    replace in one statement would hit the unique constraint (found in B14).
    """
    statement = pg_insert(RolePermission).values([row | {"role_id": role_id} for row in rows])
    await session.execute(
        statement.on_conflict_do_update(
            constraint="uq_role_permissions_role_id_feature",
            set_={
                column: statement.excluded[column]
                for column in ("view_perm", "create_perm", "edit_perm", "delete_perm")
            },
        )
    )


async def count_assignments(session: AsyncSession, role_id: int) -> int:
    return await session.scalar(
        select(func.count()).select_from(UserRole).where(UserRole.role_id == role_id)
    )


async def delete_role(session: AsyncSession, role_id: int) -> None:
    """Delete the role; its permission rows go with it (CASCADE). Raises
    IntegrityError (fk_user_roles_role_id_roles) while anyone holds it."""
    await session.execute(delete(Role).where(Role.id == role_id))
