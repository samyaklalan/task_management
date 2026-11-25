"""Database access for teams and team members. Never commits
(see app/repositories/user.py)."""

from datetime import datetime
from typing import Any

from sqlalchemy import Row, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Team, TeamMember, User, UserRole
from app.schemas.common import Pagination
from app.schemas.team import TeamMemberSort, TeamSort
from app.utils.pagination import PageParams, paginate
from app.utils.search import LIKE_ESCAPE, contains

# Members per team, computed inside the same SELECT (no N+1 over a page).
_member_count = (
    select(func.count())
    .where(TeamMember.team_id == Team.id)
    .correlate(Team)
    .scalar_subquery()
    .label("member_count")
)

_TEAM_SORT = {
    "name": (func.lower(Team.name), Team.id),
    "created_at": (Team.created_at, Team.id),
}
_MEMBER_SORT = {
    "name": (func.lower(User.name), User.id),
    "joined_at": (TeamMember.created_at, User.id),
}


# ── Teams ─────────────────────────────────────────────────────────────────────


async def list_teams(
    session: AsyncSession, *, project_id: int, q: str | None, sort: TeamSort, page: PageParams
) -> tuple[list[Row[tuple[Team, int]]], Pagination]:
    """TEAM-1: one SELECT for the page, member counts included."""
    statement: Any = (
        select(Team, _member_count).where(Team.project_id == project_id).order_by(*_TEAM_SORT[sort])
    )
    if q is not None:
        statement = statement.where(Team.name.ilike(contains(q), escape=LIKE_ESCAPE))
    return await paginate(session, statement, page, scalars=False)


async def get_with_count(
    session: AsyncSession, project_id: int, team_id: int
) -> Row[tuple[Team, int]] | None:
    """A team of THIS project (another project's team is "not found")."""
    result = await session.execute(
        select(Team, _member_count).where(Team.id == team_id, Team.project_id == project_id)
    )
    return result.first()


async def get(session: AsyncSession, project_id: int, team_id: int) -> Team | None:
    return await session.scalar(
        select(Team).where(Team.id == team_id, Team.project_id == project_id)
    )


async def create(
    session: AsyncSession, *, project_id: int, name: str, description: str | None
) -> Team:
    team = Team(project_id=project_id, name=name, description=description)
    session.add(team)
    await session.flush()
    return team


async def update_if_unchanged(
    session: AsyncSession, team_id: int, *, expected_updated_at: datetime, values: dict[str, Any]
) -> Team | None:
    """Optimistic update (api.md section 6). ``None``: changed or deleted meanwhile."""
    return await session.scalar(
        update(Team)
        .where(Team.id == team_id, Team.updated_at == expected_updated_at)
        .values(**values, updated_at=func.statement_timestamp())
        .returning(Team)
        .execution_options(populate_existing=True)
    )


async def count_members(session: AsyncSession, team_id: int) -> int:
    count = await session.scalar(
        select(func.count()).select_from(TeamMember).where(TeamMember.team_id == team_id)
    )
    return count or 0


async def delete_team(session: AsyncSession, project_id: int, team_id: int) -> bool:
    """TEAM-5. Memberships go with it (ON DELETE CASCADE)."""
    deleted = await session.scalar(
        delete(Team).where(Team.id == team_id, Team.project_id == project_id).returning(Team.id)
    )
    return deleted is not None


# ── Members ───────────────────────────────────────────────────────────────────


async def list_members(
    session: AsyncSession, *, team_id: int, sort: TeamMemberSort, page: PageParams
) -> tuple[list[Row[tuple[TeamMember, User]]], Pagination]:
    """TEAM-6: members with their names in one SELECT (users joined in)."""
    statement = (
        select(TeamMember, User)
        .join(User, User.id == TeamMember.user_id)
        .where(TeamMember.team_id == team_id)
        .order_by(*_MEMBER_SORT[sort])
    )
    return await paginate(session, statement, page, scalars=False)


async def lock_users_with_access(
    session: AsyncSession, *, organization_id: int, project_id: int, user_ids: list[int]
) -> set[int]:
    """Which of ``user_ids`` can access the project (MEM-1's rule: active, with
    an org-wide role or a role in this project).

    Their ``user_roles`` rows are locked ``FOR SHARE`` until commit, so a
    simultaneous "remove member" (MEM-4) waits for us, and then also removes
    them from this team (A-7). Locked in id order, so two adds can't deadlock.
    """
    rows = await session.scalars(
        select(UserRole.user_id)
        .join(User, User.id == UserRole.user_id)
        .where(
            UserRole.user_id.in_(user_ids),
            or_(UserRole.project_id == project_id, UserRole.project_id.is_(None)),
            User.organization_id == organization_id,
            User.is_active.is_(True),
        )
        .order_by(UserRole.id)
        .with_for_update(read=True, of=UserRole)
    )
    return set(rows)


async def add_members(session: AsyncSession, *, team_id: int, user_ids: list[int]) -> set[int]:
    """TEAM-7: one INSERT; existing memberships are skipped (ON CONFLICT DO
    NOTHING on the primary key), so a retry or a simultaneous add is harmless.
    Returns the ids actually added.

    IntegrityError: fk_team_members_team_id_teams (the team was just deleted).
    """
    statement = (
        pg_insert(TeamMember)
        .values([{"team_id": team_id, "user_id": user_id} for user_id in user_ids])
        .on_conflict_do_nothing(index_elements=[TeamMember.team_id, TeamMember.user_id])
        .returning(TeamMember.user_id)
    )
    return set(await session.scalars(statement))


async def remove_member(session: AsyncSession, *, team_id: int, user_id: int) -> bool:
    removed = await session.scalar(
        delete(TeamMember)
        .where(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
        .returning(TeamMember.user_id)
    )
    return removed is not None
