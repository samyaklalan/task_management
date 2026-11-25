"""Database access for project members: the ``user_roles`` rows that give
access to one project. Never commits (see app/repositories/user.py)."""

from typing import Any

from sqlalchemy import Row, and_, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import Role, Task, Team, TeamMember, User, UserRole
from app.schemas.common import Pagination
from app.utils.pagination import PageParams, paginate
from app.utils.search import LIKE_ESCAPE, contains


async def list_members(
    session: AsyncSession,
    *,
    organization_id: int,
    project_id: int,
    q: str | None,
    page: PageParams,
) -> tuple[list[Row[tuple[UserRole, User, Role]]], Pagination]:
    """MEM-1: everyone with access to the project, one row per user, by name.

    A user's project role wins over their org-wide role: it is the one that can
    be changed here, and a user with both appears once (no duplicate names in
    the assignee picker). Inactive users are left out.
    """
    project_row = aliased(UserRole)
    has_project_role = exists().where(
        project_row.user_id == UserRole.user_id, project_row.project_id == project_id
    )
    statement: Any = (
        select(UserRole, User, Role)
        .join(User, User.id == UserRole.user_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            User.organization_id == organization_id,
            User.is_active.is_(True),
            or_(
                UserRole.project_id == project_id,
                and_(UserRole.project_id.is_(None), ~has_project_role),
            ),
        )
        .order_by(func.lower(User.name), User.id)
    )
    if q is not None:
        statement = statement.where(User.name.ilike(contains(q), escape=LIKE_ESCAPE))
    return await paginate(session, statement, page, scalars=False)


async def add(session: AsyncSession, *, project_id: int, user_id: int, role_id: int) -> None:
    """IntegrityError: uq_user_roles_user_id_project_id (already a member) or
    fk_user_roles_role_id_roles (the role was deleted a moment ago)."""
    session.add(UserRole(user_id=user_id, role_id=role_id, project_id=project_id))
    await session.flush()


async def change_role(
    session: AsyncSession, *, project_id: int, user_id: int, role_id: int
) -> bool:
    """MEM-3. ``False``: the user has no role in this project (org-wide rows
    are never touched here). IntegrityError: the role was just deleted."""
    changed = await session.scalar(
        update(UserRole)
        .where(UserRole.user_id == user_id, UserRole.project_id == project_id)
        .values(role_id=role_id)
        .returning(UserRole.id)
    )
    return changed is not None


async def remove(session: AsyncSession, *, project_id: int, user_id: int) -> bool:
    """MEM-4. ``False``: the user has no role in this project."""
    removed = await session.scalar(
        delete(UserRole)
        .where(UserRole.user_id == user_id, UserRole.project_id == project_id)
        .returning(UserRole.id)
    )
    return removed is not None


async def unassign_tasks(session: AsyncSession, *, project_id: int, user_id: int) -> None:
    """A-7: the user's tasks in this project become unassigned. ``updated_at``
    moves on, so anyone editing one of them gets a fair ``stale_update``."""
    await session.execute(
        update(Task)
        .where(Task.project_id == project_id, Task.assignee_id == user_id)
        .values(assignee_id=None, updated_at=func.statement_timestamp())
    )


async def remove_from_teams(session: AsyncSession, *, project_id: int, user_id: int) -> None:
    """A-7: the user leaves every team of this project (one DELETE)."""
    project_teams = select(Team.id).where(Team.project_id == project_id)
    await session.execute(
        delete(TeamMember).where(
            TeamMember.user_id == user_id, TeamMember.team_id.in_(project_teams)
        )
    )
