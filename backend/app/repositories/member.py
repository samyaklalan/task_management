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


# MEM-3 change member role: everything, start to end (in my words, corrected)
# ==========================================================================
# Request: PATCH /projects/{public_id}/members/{user_id}   body {"role_id": 5}
# Changes the PROJECT role of a user who already has one in this project. 200 on success.
#
# Two different users:
#   - the CALLER (logged in): the person DOING the change -> checked by the dependency
#   - the TARGET user (user_id from the PATH): whose role changes -> checked by the service
#
# 1. DEPENDENCIES (run before the route):
#    - user_id: IdPath -> from the URL path
#    - body: ChangeMemberRoleRequest -> ONLY role_id (the new role); unknown field -> 422
#    - project: Annotated[ProjectContext, require_project_permission(MEMBERS, EDIT)]
#        get_project_context: caller can see the project (any org or project role,
#          not specifically "view")                                 -> else 404
#        guard: caller's role has members.edit                      -> else 403
#        guard: it's a write, so not allowed in an archived project -> else 409
#        returns ProjectContext -> project.project = the Project
#
# 2. SERVICE change_role(session, project.project, user_id, data):
#    a) role = _role(session, project.organization_id, data.role_id)
#       -> role_repository.get_in_organization: role EXISTS and is in the SAME ORG
#       None -> InvalidReferenceError on role_id
#    b) user = user_repository.get_in_organization(session, project.organization_id,
#                                                  user_id)
#       -> user EXISTS and is in the SAME ORG
#       None -> 404 "Member" (NOT invalid reference: the user is in the URL, not the
#       body; unlike MEM-2). No is_active check here.
#    c) async with session.begin_nested():   -> SAVEPOINT
#           changed = member_repository.change_role(project_id=project.id,
#                                                   user_id=user_id, role_id=role.id)
#       except IntegrityError -> the role was DELETED between (a) and the UPDATE
#         (foreign key refuses it) -> InvalidReferenceError on role_id
#         (role.id is never null; the risk is the role disappearing in between)
#    d) if not changed -> 404 "Member": the user has NO role in this project
#       (only an org-wide role, which is changed by USR-6, not here)
#    e) session.commit()
#    f) return _item(user, role, project_scope=True)
#       -> ONE MemberOut {user: UserRef(id, name), role: RoleRef(id, name),
#                         scope: "project"}  (built from user/role the service
#                         already has; the repository doesn't return the object)
#
# 3. REPOSITORY change_role (app/repositories/member.py):
#    needs project_id (which project), user_id (whose role), role_id (the new role)
#      UPDATE user_roles SET role_id = :role_id
#      WHERE user_id = :user_id AND project_id = :project_id
#      RETURNING id
#    - only THIS project's row: an org-wide row (project_id NULL) is never touched
#    - returns changed is not None: an id came back -> True, nothing -> False
#      (RETURNING gives the row's id, not the whole UserRole object)
#
# NOTE: both get_in_organization functions (user + role) are NOT written yet, so this
# route currently fails with a 500 at step 2a.


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


# remove_from_teams (MEM-4, decision A-7), in my words:
#   DELETE FROM team_members
#   WHERE team_members.user_id = :user_id            -- the REMOVED member
#     AND team_members.team_id IN (                  -- only teams of THIS project
#           SELECT teams.id FROM teams
#           WHERE teams.project_id = :project_id
#     );
#
#   team_members links users to teams (team_id, user_id).
#   ONE DELETE removes the user from EVERY team of this project.
#   Their teams in OTHER projects are untouched.
#   e.g. removing Ravi from project 12:
#     Backend (project 12) -> deleted, Frontend (project 12) -> deleted,
#     QA (project 5) -> kept
async def lock_users_with_access(
    session: AsyncSession, *, organization_id: int, project_id: int, user_ids: list[int]
) -> set[int]:
    """Which of ``user_ids`` can access the project (MEM-1's rule: active, with
    an org-wide role or a role in this project).

    Their ``user_roles`` rows are locked ``FOR SHARE`` until commit, so a
    simultaneous "remove member" (MEM-4) waits for us and then also undoes what
    we did (A-7: leaves the team, task unassigned). Locked in id order, so two
    callers can't deadlock. Used by TEAM-7 and by task assignment.
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
