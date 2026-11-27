"""The database's own rules, proven against PostgreSQL (docs/database.md, section 3).

The application will check most of these rules too, but the database is the
last line of defence: if a bug or a race slips past the code, these constraints
still refuse the bad data. So every UNIQUE, CHECK and ON DELETE rule in
database.md has a test here, and each test checks that it was rejected by the
right constraint (by name), not just that "something" failed.

All rows are rolled back after each test (tests/conftest.py).
"""

from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Organization,
    PasswordResetToken,
    Project,
    RefreshToken,
    Role,
    RolePermission,
    Space,
    Task,
    TaskComment,
    TaskStatus,
    Team,
    TeamMember,
    User,
    UserRole,
)
from tests.factories import (
    make_organization,
    make_project,
    make_role,
    make_space,
    make_task,
    make_task_comment,
    make_task_status,
    make_team,
    make_user,
)

TOKEN_HASH = "a" * 64  # SHA-256 hex length; the value itself doesn't matter here


def rejected(constraint: str) -> AbstractContextManager[Any]:
    """Expect PostgreSQL to refuse the statement because of this exact constraint."""
    # PostgreSQL quotes the constraint name in the message, e.g.
    # duplicate key value violates unique constraint "uq_users_email"
    return pytest.raises(IntegrityError, match=f'"{constraint}"')


async def add(session: AsyncSession, row: Any) -> Any:
    session.add(row)
    await session.flush()
    return row


async def count(session: AsyncSession, model: type, *where: Any) -> int:
    return await session.scalar(select(func.count()).select_from(model).where(*where))


def in_one_hour() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


# --------------------------------------------------------------------------
# UNIQUE
# --------------------------------------------------------------------------


async def test_organization_slug_is_unique(db_session: AsyncSession) -> None:
    await make_organization(db_session, slug="acme")
    with rejected("uq_organizations_slug"):
        await make_organization(db_session, slug="acme")


async def test_email_is_unique_across_all_organizations(db_session: AsyncSession) -> None:
    # P-8: one email = one account in one organization, system-wide.
    await make_user(db_session, email="asha@example.com")
    with rejected("uq_users_email"):
        await make_user(db_session, email="asha@example.com")  # a different organization


async def test_project_public_id_is_unique(db_session: AsyncSession) -> None:
    await make_project(db_session, public_id="abcdef0123456789")
    with rejected("uq_projects_public_id"):
        await make_project(db_session, public_id="abcdef0123456789")


async def test_task_prefix_is_unique_within_an_organization(db_session: AsyncSession) -> None:
    organization = await make_organization(db_session)
    await make_project(db_session, organization=organization, task_prefix="WEB")
    with rejected("uq_projects_organization_id_task_prefix"):
        await make_project(db_session, organization=organization, task_prefix="WEB")


async def test_same_task_prefix_is_allowed_in_another_organization(
    db_session: AsyncSession,
) -> None:
    await make_project(db_session, task_prefix="WEB")
    await make_project(db_session, task_prefix="WEB")  # each call makes a new organization

    assert await count(db_session, Project, Project.task_prefix == "WEB") == 2


async def test_space_name_is_unique_within_a_project(db_session: AsyncSession) -> None:
    project = await make_project(db_session)
    await make_space(db_session, project=project, name="Backend")
    with rejected("uq_spaces_project_id_name"):
        await make_space(db_session, project=project, name="Backend")


async def test_same_space_name_is_allowed_in_another_project(db_session: AsyncSession) -> None:
    await make_space(db_session, name="General")
    await make_space(db_session, name="General")  # each call makes a new project

    assert await count(db_session, Space, Space.name == "General") == 2


async def test_task_number_is_unique_within_a_project(db_session: AsyncSession) -> None:
    project = await make_project(db_session)
    await make_task(db_session, project=project, number=7)
    with rejected("uq_tasks_project_id_number"):
        await make_task(db_session, project=project, number=7)


async def test_one_permission_row_per_feature_per_role(db_session: AsyncSession) -> None:
    role = await make_role(db_session)
    await add(db_session, RolePermission(role_id=role.id, feature="tasks"))
    with rejected("uq_role_permissions_role_id_feature"):
        await add(db_session, RolePermission(role_id=role.id, feature="tasks"))


async def test_one_role_per_user_per_project(db_session: AsyncSession) -> None:
    project = await make_project(db_session)
    user = await make_user(db_session)
    role = await make_role(db_session)
    await add(db_session, UserRole(user_id=user.id, role_id=role.id, project_id=project.id))
    with rejected("uq_user_roles_user_id_project_id"):
        await add(db_session, UserRole(user_id=user.id, role_id=role.id, project_id=project.id))


async def test_at_most_one_organization_wide_role_per_user(db_session: AsyncSession) -> None:
    # NULLS NOT DISTINCT (P-6): two rows with project_id NULL count as duplicates.
    # A plain UNIQUE would treat NULL != NULL and let both in.
    user = await make_user(db_session)
    role = await make_role(db_session)
    await add(db_session, UserRole(user_id=user.id, role_id=role.id, project_id=None))
    with rejected("uq_user_roles_user_id_project_id"):
        await add(db_session, UserRole(user_id=user.id, role_id=role.id, project_id=None))


async def test_user_can_hold_roles_in_different_projects(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    role = await make_role(db_session)
    first, second = await make_project(db_session), await make_project(db_session)
    for project_id in (None, first.id, second.id):
        await add(db_session, UserRole(user_id=user.id, role_id=role.id, project_id=project_id))

    assert await count(db_session, UserRole, UserRole.user_id == user.id) == 3


async def test_user_is_in_a_team_only_once(db_session: AsyncSession) -> None:
    team = await make_team(db_session)
    user = await make_user(db_session)
    await add(db_session, TeamMember(team_id=team.id, user_id=user.id))
    db_session.expunge_all()  # else the ORM itself refuses a second object with the same key
    with rejected("pk_team_members"):
        await add(db_session, TeamMember(team_id=team.id, user_id=user.id))


@pytest.mark.parametrize(
    ("model", "constraint"),
    [
        (RefreshToken, "uq_refresh_tokens_token_hash"),
        (PasswordResetToken, "uq_password_reset_tokens_token_hash"),
    ],
)
async def test_token_hash_is_unique(
    db_session: AsyncSession, model: type[RefreshToken | PasswordResetToken], constraint: str
) -> None:
    user = await make_user(db_session)
    await add(db_session, model(user_id=user.id, token_hash=TOKEN_HASH, expires_at=in_one_hour()))
    with rejected(constraint):
        await add(
            db_session, model(user_id=user.id, token_hash=TOKEN_HASH, expires_at=in_one_hour())
        )


# --------------------------------------------------------------------------
# CHECK
# --------------------------------------------------------------------------

TODAY = date(2026, 11, 19)
YESTERDAY = TODAY - timedelta(days=1)


def _case_id(value: Any) -> str:
    """Readable test ids, e.g. ``[task_status-color=red-ck_task_statuses_color_format]``."""
    if callable(value):
        return value.__name__.removeprefix("make_")
    if isinstance(value, dict):
        return ",".join(f"{key}={val}" for key, val in value.items())
    return str(value)


@pytest.mark.parametrize(
    ("factory", "values", "constraint"),
    [
        # organizations.slug: lowercase words joined by single hyphens
        (make_organization, {"slug": "Acme"}, "ck_organizations_slug_format"),
        (make_organization, {"slug": "acme corp"}, "ck_organizations_slug_format"),
        (make_organization, {"slug": "-acme"}, "ck_organizations_slug_format"),
        (make_organization, {"slug": "acme--corp"}, "ck_organizations_slug_format"),
        (make_organization, {"slug": "acme-"}, "ck_organizations_slug_format"),
        # users.email: stored lowercase
        (make_user, {"email": "Asha@example.com"}, "ck_users_email_lowercase"),
        # projects
        (make_project, {"status": "deleted"}, "ck_projects_status"),
        (make_project, {"task_prefix": "web"}, "ck_projects_task_prefix_format"),
        (make_project, {"task_prefix": "W"}, "ck_projects_task_prefix_format"),
        (make_project, {"task_prefix": "1WEB"}, "ck_projects_task_prefix_format"),
        (make_project, {"task_prefix": "WEB-1"}, "ck_projects_task_prefix_format"),
        (make_project, {"next_task_number": 0}, "ck_projects_next_task_number_positive"),
        (
            make_project,
            {"start_date": TODAY, "end_date": YESTERDAY},
            "ck_projects_date_range",
        ),
        # spaces
        (make_space, {"position": -1}, "ck_spaces_position_non_negative"),
        # task_statuses
        (make_task_status, {"category": "blocked"}, "ck_task_statuses_category"),
        (make_task_status, {"color": "red"}, "ck_task_statuses_color_format"),
        (make_task_status, {"color": "#12345"}, "ck_task_statuses_color_format"),
        (make_task_status, {"color": "#GGGGGG"}, "ck_task_statuses_color_format"),
        (make_task_status, {"position": -1}, "ck_task_statuses_position_non_negative"),
        # tasks
        (make_task, {"priority": "critical"}, "ck_tasks_priority"),
        (make_task, {"number": 0}, "ck_tasks_number_positive"),
        (make_task, {"start_date": TODAY, "end_date": YESTERDAY}, "ck_tasks_date_range"),
    ],
    ids=_case_id,
)
async def test_check_constraint_rejects_invalid_value(
    db_session: AsyncSession, factory: Any, values: dict[str, Any], constraint: str
) -> None:
    with rejected(constraint):
        await factory(db_session, **values)


async def test_check_constraints_accept_the_boundary_values(db_session: AsyncSession) -> None:
    """The other side of the CHECK tests: valid edge cases must NOT be rejected."""
    await make_organization(db_session, slug="a")
    await make_organization(db_session, slug="acme-corp-2")
    await make_project(db_session, task_prefix="AB")  # shortest allowed
    await make_project(db_session, task_prefix="A123456789")  # longest allowed (10)
    for status in ("active", "on_hold", "completed", "archived"):
        await make_project(db_session, status=status)
    await make_project(db_session, start_date=TODAY, end_date=TODAY)  # same day is fine
    await make_project(db_session, start_date=TODAY)  # open-ended
    await make_space(db_session, position=0)
    for category in ("todo", "in_progress", "done"):
        await make_task_status(db_session, category=category, color="#a1B2c3")
    for priority in ("low", "medium", "high", "urgent"):
        await make_task(db_session, priority=priority, start_date=TODAY, end_date=TODAY)


# --------------------------------------------------------------------------
# ON DELETE
# --------------------------------------------------------------------------


async def test_organization_with_members_cannot_be_deleted(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    with rejected("fk_users_organization_id_organizations"):
        await db_session.execute(
            delete(Organization).where(Organization.id == user.organization_id)
        )


async def test_status_in_use_cannot_be_deleted(db_session: AsyncSession) -> None:
    # The API turns this into 409 Conflict: move the tasks to another status first.
    task = await make_task(db_session)
    with rejected("fk_tasks_status_id_task_statuses"):
        await db_session.execute(delete(TaskStatus).where(TaskStatus.id == task.status_id))


async def test_unused_status_can_be_deleted(db_session: AsyncSession) -> None:
    status = await make_task_status(db_session)
    await db_session.execute(delete(TaskStatus).where(TaskStatus.id == status.id))

    assert await count(db_session, TaskStatus, TaskStatus.id == status.id) == 0


async def test_space_with_tasks_cannot_be_deleted(db_session: AsyncSession) -> None:
    task = await make_task(db_session)
    with rejected("fk_tasks_space_id_spaces"):
        await db_session.execute(delete(Space).where(Space.id == task.space_id))


async def test_task_creator_cannot_be_deleted(db_session: AsyncSession) -> None:
    task = await make_task(db_session)
    with rejected("fk_tasks_created_by_users"):
        await db_session.execute(delete(User).where(User.id == task.created_by))


async def test_comment_author_cannot_be_deleted(db_session: AsyncSession) -> None:
    task = await make_task(db_session)
    author = await make_user(db_session)
    await make_task_comment(db_session, task=task, author=author)
    with rejected("fk_task_comments_author_id_users"):
        await db_session.execute(delete(User).where(User.id == author.id))


async def test_role_still_assigned_cannot_be_deleted(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    role = await make_role(db_session)
    await add(db_session, UserRole(user_id=user.id, role_id=role.id))
    with rejected("fk_user_roles_role_id_roles"):
        await db_session.execute(delete(Role).where(Role.id == role.id))


async def test_deleting_the_assignee_unassigns_the_task(db_session: AsyncSession) -> None:
    assignee = await make_user(db_session)
    task = await make_task(db_session, assignee_id=assignee.id)

    await db_session.execute(delete(User).where(User.id == assignee.id))

    assert await count(db_session, Task, Task.id == task.id) == 1
    assert await count(db_session, Task, Task.id == task.id, Task.assignee_id.is_(None)) == 1


async def test_deleting_a_user_removes_their_tokens_memberships_and_roles(
    db_session: AsyncSession,
) -> None:
    user = await make_user(db_session)
    team = await make_team(db_session)
    role = await make_role(db_session)
    await add(
        db_session, RefreshToken(user_id=user.id, token_hash=TOKEN_HASH, expires_at=in_one_hour())
    )
    await add(
        db_session,
        PasswordResetToken(user_id=user.id, token_hash=TOKEN_HASH, expires_at=in_one_hour()),
    )
    await add(db_session, TeamMember(team_id=team.id, user_id=user.id))
    await add(db_session, UserRole(user_id=user.id, role_id=role.id))

    await db_session.execute(delete(User).where(User.id == user.id))

    for model in (RefreshToken, PasswordResetToken, TeamMember, UserRole):
        assert await count(db_session, model, model.user_id == user.id) == 0, model.__name__
    # The team and the role themselves stay.
    assert await count(db_session, Team, Team.id == team.id) == 1
    assert await count(db_session, Role, Role.id == role.id) == 1


async def test_deleting_a_team_removes_its_memberships_only(db_session: AsyncSession) -> None:
    team = await make_team(db_session)
    user = await make_user(db_session)
    await add(db_session, TeamMember(team_id=team.id, user_id=user.id))

    await db_session.execute(delete(Team).where(Team.id == team.id))

    assert await count(db_session, TeamMember, TeamMember.team_id == team.id) == 0
    assert await count(db_session, User, User.id == user.id) == 1


async def test_deleting_a_role_removes_its_permissions(db_session: AsyncSession) -> None:
    role = await make_role(db_session)
    await add(db_session, RolePermission(role_id=role.id, feature="tasks", view_perm=True))

    await db_session.execute(delete(Role).where(Role.id == role.id))

    assert await count(db_session, RolePermission, RolePermission.role_id == role.id) == 0


async def test_deleting_a_task_removes_its_comments(db_session: AsyncSession) -> None:
    comment = await make_task_comment(db_session)

    await db_session.execute(delete(Task).where(Task.id == comment.task_id))

    assert await count(db_session, TaskComment, TaskComment.task_id == comment.task_id) == 0


async def test_deleting_a_project_removes_everything_inside_it(db_session: AsyncSession) -> None:
    """A hard project delete (not offered by the API, which only archives) must
    succeed in one statement, although tasks.status_id and tasks.space_id block
    deleting a status or space that tasks still use: the cascade removes the
    tasks too. (Which action those two keys use, NO ACTION rather than RESTRICT,
    is checked separately in test_every_foreign_key_has_the_documented_on_delete_rule.)"""
    project = await make_project(db_session)
    space = await make_space(db_session, project=project)
    status = await make_task_status(db_session, project=project)
    task = await make_task(db_session, project=project, space=space, status=status)
    await make_task_comment(db_session, task=task)
    team = await make_team(db_session, project=project)
    member = await make_user(db_session)
    await add(db_session, TeamMember(team_id=team.id, user_id=member.id))
    role = await make_role(db_session)
    await add(db_session, UserRole(user_id=member.id, role_id=role.id, project_id=project.id))
    other_task = await make_task(db_session)  # in another project: must survive

    await db_session.execute(delete(Project).where(Project.id == project.id))

    for model, where in [
        (Space, Space.project_id == project.id),
        (TaskStatus, TaskStatus.project_id == project.id),
        (Task, Task.project_id == project.id),
        (TaskComment, TaskComment.task_id == task.id),
        (Team, Team.project_id == project.id),
        (TeamMember, TeamMember.team_id == team.id),
        (UserRole, UserRole.project_id == project.id),
    ]:
        assert await count(db_session, model, where) == 0, model.__name__
    # People, roles and other projects are untouched.
    assert await count(db_session, User, User.id == member.id) == 1
    assert await count(db_session, Role, Role.id == role.id) == 1
    assert await count(db_session, Task, Task.id == other_task.id) == 1


# What each foreign key does when the row it points to is deleted
# (database.md, "Delete rules"). PostgreSQL's codes: a = NO ACTION, c = CASCADE,
# n = SET NULL, r = RESTRICT.
EXPECTED_ON_DELETE = {
    "fk_password_reset_tokens_user_id_users": "c",
    "fk_projects_organization_id_organizations": "a",
    "fk_refresh_tokens_user_id_users": "c",
    "fk_role_permissions_role_id_roles": "c",
    "fk_roles_organization_id_organizations": "a",
    "fk_spaces_project_id_projects": "c",
    "fk_task_comments_author_id_users": "a",
    "fk_task_comments_task_id_tasks": "c",
    "fk_task_statuses_project_id_projects": "c",
    "fk_tasks_assignee_id_users": "n",
    "fk_tasks_created_by_users": "a",
    "fk_tasks_project_id_projects": "c",
    "fk_tasks_space_id_spaces": "a",
    "fk_tasks_status_id_task_statuses": "a",
    "fk_team_members_team_id_teams": "c",
    "fk_team_members_user_id_users": "c",
    "fk_teams_project_id_projects": "c",
    "fk_user_roles_project_id_projects": "c",
    "fk_user_roles_role_id_roles": "a",
    "fk_user_roles_user_id_users": "c",
    "fk_users_organization_id_organizations": "a",
}


async def test_every_foreign_key_has_the_documented_on_delete_rule(
    db_session: AsyncSession,
) -> None:
    """Reads the rules from PostgreSQL's catalog. This catches what the behaviour
    tests above can't: RESTRICT and NO ACTION both block a direct delete, so only
    the catalog shows which one is in place (database.md explains why NO ACTION
    was chosen for tasks.status_id and tasks.space_id)."""
    rows = await db_session.execute(
        text(
            "SELECT conname, confdeltype::text FROM pg_constraint "
            "WHERE contype = 'f' AND connamespace = 'public'::regnamespace"
        )
    )
    actual = {name: action for name, action in rows}

    assert actual == EXPECTED_ON_DELETE
