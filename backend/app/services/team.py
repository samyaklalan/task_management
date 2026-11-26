"""Teams of a project (docs/api.md, TEAM-1 to TEAM-8)."""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InvalidReferenceError, NotFoundError, StaleUpdateError
from app.models import Project, Team
from app.repositories import team as team_repository
from app.schemas.common import Page, UserRef
from app.schemas.team import (
    AddTeamMembersRequest,
    AddTeamMembersResponse,
    CreateTeamRequest,
    TeamMemberOut,
    TeamMemberSort,
    TeamOut,
    TeamSort,
    UpdateTeamRequest,
)
from app.utils.pagination import PageParams

# ── TEAM-1 to TEAM-5: teams ───────────────────────────────────────────────────


async def list_teams(
    session: AsyncSession,
    project: Project,
    *,
    q: str | None,
    sort: TeamSort,
    page: PageParams,
) -> Page[TeamOut]:
    rows, pagination = await team_repository.list_teams(
        session, project_id=project.id, q=q, sort=sort, page=page
    )
    return Page[TeamOut](items=[_out(team, count) for team, count in rows], pagination=pagination)


async def get_team(session: AsyncSession, project: Project, team_id: int) -> TeamOut:
    row = await team_repository.get_with_count(session, project.id, team_id)
    if row is None:
        raise NotFoundError("Team")
    team, count = row
    return _out(team, count)


async def create_team(session: AsyncSession, project: Project, data: CreateTeamRequest) -> TeamOut:
    team = await team_repository.create(
        session, project_id=project.id, name=data.name, description=data.description
    )
    await session.commit()
    return _out(team, 0)


async def update_team(
    session: AsyncSession, project: Project, team_id: int, data: UpdateTeamRequest
) -> TeamOut:
    """Name and/or description, only if nobody saved in between (409
    ``stale_update`` with the current team in ``details.current``)."""
    team = await _get(session, project, team_id)
    if team.updated_at != data.expected_updated_at:
        raise StaleUpdateError(current=await _current(session, team))

    values = data.model_dump(include=data.model_fields_set - {"expected_updated_at"})
    if values:
        updated = await team_repository.update_if_unchanged(
            session,
            team.id,
            expected_updated_at=data.expected_updated_at,
            values=values,
        )
        if updated is None:
            # Saved (or deleted) by someone else between our SELECT and UPDATE.
            # Nothing was written, so no rollback is needed.
            current = await team_repository.get(session, project.id, team_id)
            if current is None:
                raise NotFoundError("Team")
            raise StaleUpdateError(current=await _current(session, current))
        team = updated
        await session.commit()
    return _out(team, await team_repository.count_members(session, team.id))


async def delete_team(session: AsyncSession, project: Project, team_id: int) -> None:
    """Memberships go with the team (CASCADE); users and tasks are unaffected."""
    if not await team_repository.delete_team(session, project.id, team_id):
        raise NotFoundError("Team")
    await session.commit()


# ── TEAM-6 to TEAM-8: members ─────────────────────────────────────────────────


async def list_members(
    session: AsyncSession,
    project: Project,
    team_id: int,
    *,
    sort: TeamMemberSort,
    page: PageParams,
) -> Page[TeamMemberOut]:
    await _get(session, project, team_id)
    rows, pagination = await team_repository.list_members(
        session, team_id=team_id, sort=sort, page=page
    )
    items = [
        TeamMemberOut(user=UserRef(id=user.id, name=user.name), joined_at=membership.created_at)
        for membership, user in rows
    ]
    return Page[TeamMemberOut](items=items, pagination=pagination)


async def add_members(
    session: AsyncSession, project: Project, team_id: int, data: AddTeamMembersRequest
) -> AddTeamMembersResponse:
    """All or nothing: if any user can't access the project, nobody is added."""
    team = await _get(session, project, team_id)
    with_access = await team_repository.lock_users_with_access(
        session,
        organization_id=project.organization_id,
        project_id=project.id,
        user_ids=data.user_ids,
    )
    refused = [user_id for user_id in data.user_ids if user_id not in with_access]
    if refused:
        raise InvalidReferenceError(
            "user_ids",
            "These users can't access this project: " + ", ".join(map(str, refused)),
        )

    try:
        # A savepoint: a refused insert undoes only itself (see member.py).
        async with session.begin_nested():
            added = await team_repository.add_members(
                session, team_id=team.id, user_ids=data.user_ids
            )
    except IntegrityError as exc:  # the team was deleted a moment ago
        raise NotFoundError("Team") from exc
    await session.commit()
    return AddTeamMembersResponse(
        added=[user_id for user_id in data.user_ids if user_id in added],
        skipped=[user_id for user_id in data.user_ids if user_id not in added],
    )


async def remove_member(
    session: AsyncSession, project: Project, team_id: int, user_id: int
) -> None:
    team = await _get(session, project, team_id)
    if not await team_repository.remove_member(session, team_id=team.id, user_id=user_id):
        raise NotFoundError("Team member")
    await session.commit()


# ── Shared ────────────────────────────────────────────────────────────────────


async def _get(session: AsyncSession, project: Project, team_id: int) -> Team:
    team = await team_repository.get(session, project.id, team_id)
    if team is None:
        raise NotFoundError("Team")
    return team


async def _current(session: AsyncSession, team: Team) -> dict:
    count = await team_repository.count_members(session, team.id)
    return _out(team, count).model_dump(mode="json")


def _out(team: Team, member_count: int) -> TeamOut:
    return TeamOut(
        id=team.id,
        name=team.name,
        description=team.description,
        member_count=member_count,
        created_at=team.created_at,
        updated_at=team.updated_at,
    )
