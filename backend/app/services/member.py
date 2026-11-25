"""Project members (docs/api.md, MEM-1 to MEM-4)."""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AlreadyMemberError, InvalidReferenceError, NotFoundError
from app.db.errors import constraint_name
from app.models import Project, Role, User
from app.repositories import member as member_repository
from app.repositories import role as role_repository
from app.repositories import user as user_repository
from app.schemas.common import Page, RoleRef, UserRef
from app.schemas.member import AddMemberRequest, ChangeMemberRoleRequest, MemberOut
from app.utils.pagination import PageParams

_UNKNOWN_ROLE = "This role doesn't exist in your organization."
_UNKNOWN_USER = "This user doesn't exist in your organization or is deactivated."


# ── MEM-1 list ────────────────────────────────────────────────────────────────

# get_context checks does project belong tonyiu and ur iole
#  is allowed tosee it or not nad then load all the permiisons
# you having and return prohect objct based
# likeget_curent use token and return the logged in
# user here it take and return project
# so we can get org and id of project


async def list_members(
    # gettingprohject fform the dependency
    session: AsyncSession,
    project: Project,
    *,
    q: str | None,
    page: PageParams,
) -> Page[MemberOut]:
    rows, pagination = await member_repository.list_members(
        session,
        organization_id=project.organization_id,
        project_id=project.id,
        q=q,
        page=page,
    )
    items = [
        _item(user, role, project_scope=row.project_id is not None) for row, user, role in rows
    ]
    return Page[MemberOut](items=items, pagination=pagination)


# ── MEM-2 add ─────────────────────────────────────────────────────────────────


async def add_member(session: AsyncSession, project: Project, data: AddMemberRequest) -> MemberOut:
    user = await user_repository.get_in_organization(session, project.organization_id, data.user_id)
    # user exist and its in same organization
    if user is None or not user.is_active:
        raise InvalidReferenceError("user_id", _UNKNOWN_USER)
    role = await _role(session, project.organization_id, data.role_id)
    # roleexist and its in same orgnization
    try:
        # A savepoint: a refused insert undoes only itself, so the objects
        # already loaded in this session stay usable.
        async with session.begin_nested():
            await member_repository.add(
                session, project_id=project.id, user_id=user.id, role_id=role.id
            )
    except IntegrityError as exc:
        # Two simultaneous adds: the unique constraint lets exactly one win.
        if constraint_name(exc) == "uq_user_roles_user_id_project_id":
            raise AlreadyMemberError() from exc
        raise InvalidReferenceError("role_id", _UNKNOWN_ROLE) from exc  # role just deleted
    await session.commit()
    return _item(user, role, project_scope=True)


# ── MEM-3 change role ─────────────────────────────────────────────────────────


async def change_role(
    session: AsyncSession, project: Project, user_id: int, data: ChangeMemberRoleRequest
) -> MemberOut:
    """Only the project role can be changed here; org-wide roles are USR-6's."""
    role = await _role(session, project.organization_id, data.role_id)
    user = await user_repository.get_in_organization(session, project.organization_id, user_id)
    if user is None:  # another organization's user can't have a role here
        raise NotFoundError("Member")
    try:
        async with session.begin_nested():
            changed = await member_repository.change_role(
                session, project_id=project.id, user_id=user_id, role_id=role.id
            )
    except IntegrityError as exc:  # the role was deleted a moment ago
        raise InvalidReferenceError("role_id", _UNKNOWN_ROLE) from exc
    if not changed:  # no project role (an org-wide role alone isn't changed here)
        raise NotFoundError("Member")
    await session.commit()
    return _item(user, role, project_scope=True)


# ── MEM-4 remove ──────────────────────────────────────────────────────────────


async def remove_member(session: AsyncSession, project: Project, user_id: int) -> None:
    """Remove the project role and, in the same transaction (A-7), unassign the
    user's tasks in this project and take them out of its teams."""
    if not await member_repository.remove(session, project_id=project.id, user_id=user_id):
        raise NotFoundError("Member")
    await member_repository.unassign_tasks(session, project_id=project.id, user_id=user_id)
    await member_repository.remove_from_teams(session, project_id=project.id, user_id=user_id)
    await session.commit()


# MEM-4 remove member: everything, start to end (in my words, corrected)
# =====================================================================
# Request: DELETE /projects/{public_id}/members/{user_id}   (no body)
# Removes a user's PROJECT role; also unassigns their tasks here and takes them
# out of this project's teams. 204 No Content on success (returns nothing).
#
# 1. DEPENDENCIES (run before the route):
#    - user_id: IdPath -> from the URL path; a bad id -> 422
#    - session: SessionDep -> the DB connection
#    - project: Annotated[ProjectContext, require_project_permission(MEMBERS, DELETE)]
#        get_project_context: caller can see the project            -> else 404
#        guard: caller's role has members.delete                    -> else 403
#        guard: it's a write, so not allowed in an archived project -> else 409
#    - summary / responses (403, 404, 409 project_archived) document the errors;
#      the error body carries code / message / details.
#
# 2. SERVICE remove_member(session, project.project, user_id), ONE transaction (A-7):
#    a) member_repository.remove(project_id=project.id, user_id=user_id)
#         DELETE FROM user_roles
#         WHERE user_id = :user_id AND project_id = :project_id
#         RETURNING id
#       -> return removed is not None: a row deleted -> True, nothing -> False
#       False -> 404 "Member" (no role in this project)
#       Only the PROJECT row is deleted; an org-wide role (project_id NULL) is never
#       touched here, so a user with only an org-wide role gets 404.
#    b) member_repository.unassign_tasks(project_id, user_id)
#       -> their tasks in THIS project become unassigned (bulk UPDATE, so it sets
#          updated_at itself, rule 8)
#    c) member_repository.remove_from_teams(project_id, user_id)
#       -> they leave every team of THIS project (one DELETE)
#    d) session.commit() -> a, b, c saved TOGETHER, or none of them
#
# 3. RESPONSE: 204, empty body.
#
# Unlike MEM-2 / MEM-3, no get_in_organization call here, so this route is NOT
# affected by the missing get_in_organization functions.
# ── Shared ────────────────────────────────────────────────────────────────────


async def _role(session: AsyncSession, organization_id: int, role_id: int) -> Role:
    role = await role_repository.get_in_organization(session, organization_id, role_id)
    if role is None:  # unknown, or another organization's role: same answer
        raise InvalidReferenceError("role_id", _UNKNOWN_ROLE)
    return role


def _item(user: User, role: Role, *, project_scope: bool) -> MemberOut:
    return MemberOut(
        user=UserRef(id=user.id, name=user.name),
        role=RoleRef(id=role.id, name=role.name),
        scope="project" if project_scope else "organization",
    )
