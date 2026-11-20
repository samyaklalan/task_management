"""Database access for roles, their permissions and role assignments.
Never commits (see app/repositories/user.py)."""

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Role, RolePermission, UserRole


async def create(session: AsyncSession, *, organization_id: int, name: str) -> Role:
    role = Role(organization_id=organization_id, name=name)
    session.add(role)
    await session.flush()
    return role


async def add_permissions(session: AsyncSession, role_id: int, rows: list[dict]) -> None:
    """Insert permission rows (``{"feature": ..., "view_perm": ..., ...}``) in one statement."""
    rows_with_role = []
    for row in rows:
        new_row = dict(row)  # copy, so the caller's row isn't changed
        new_row["role_id"] = role_id  # add the role this permission belongs to
        rows_with_role.append(new_row)

    await session.execute(insert(RolePermission), rows_with_role)  # one INSERT for all rows


async def assign_org_role(session: AsyncSession, *, user_id: int, role_id: int) -> None:
    """Give the user their organization-wide role (``project_id`` NULL).

    A second org-wide role for the same user fails on
    ``uq_user_roles_user_id_project_id`` (NULLS NOT DISTINCT).
    """
    session.add(UserRole(user_id=user_id, role_id=role_id, project_id=None))
    await session.flush()
