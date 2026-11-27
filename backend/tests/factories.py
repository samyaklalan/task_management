"""Helpers that insert valid rows, so a test only spells out what it's about.

Each ``make_*`` function adds one row, flushes it (so it has an id and its
server defaults) and returns it. Anything not passed gets a valid, unique
default; missing parents are created automatically:

    task = await make_task(db_session)                    # creates org, user,
                                                          # project, space, status
    task = await make_task(db_session, project=p, title="Fix login")
    await make_space(db_session, project=p, name="Backend")

Keyword arguments override column values, e.g. ``make_project(s, status="archived")``.

Rows are only flushed, never committed: the ``db_session`` fixture rolls them
back after each test. ``password_hash`` is a placeholder, not a real Argon2 hash;
tests that log in will hash real passwords with app.core.security.
"""

import itertools
import secrets
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Organization,
    Project,
    Role,
    Space,
    Task,
    TaskComment,
    TaskStatus,
    Team,
    User,
)

# One counter for the whole run keeps names, slugs, emails and prefixes unique.
_sequence = itertools.count(1)

PLACEHOLDER_PASSWORD_HASH = "not-a-real-hash"  # noqa: S105  (test data, not a secret)


def _next() -> int:
    return next(_sequence)


async def _add[T](session: AsyncSession, row: T) -> T:
    session.add(row)
    await session.flush()
    return row


async def make_organization(session: AsyncSession, **values: Any) -> Organization:
    n = _next()
    defaults = {"name": f"Organization {n}", "slug": f"org-{n}"}
    return await _add(session, Organization(**(defaults | values)))


async def make_user(
    session: AsyncSession, *, organization: Organization | None = None, **values: Any
) -> User:
    organization = organization or await make_organization(session)
    n = _next()
    defaults = {
        "organization_id": organization.id,
        "name": f"User {n}",
        "email": f"user{n}@example.com",
        "password_hash": PLACEHOLDER_PASSWORD_HASH,
    }
    return await _add(session, User(**(defaults | values)))


async def make_project(
    session: AsyncSession, *, organization: Organization | None = None, **values: Any
) -> Project:
    organization = organization or await make_organization(session)
    n = _next()
    defaults = {
        "organization_id": organization.id,
        "public_id": secrets.token_hex(8),  # 16 characters, like the real ones
        "name": f"Project {n}",
        "task_prefix": f"P{n}",  # matches ^[A-Z][A-Z0-9]{1,9}$
    }
    return await _add(session, Project(**(defaults | values)))


async def make_space(
    session: AsyncSession, *, project: Project | None = None, **values: Any
) -> Space:
    project = project or await make_project(session)
    n = _next()
    defaults = {"project_id": project.id, "name": f"Space {n}", "position": 0}
    return await _add(session, Space(**(defaults | values)))


async def make_task_status(
    session: AsyncSession, *, project: Project | None = None, **values: Any
) -> TaskStatus:
    project = project or await make_project(session)
    n = _next()
    defaults = {
        "project_id": project.id,
        "name": f"Status {n}",
        "color": "#6B7280",
        "position": 0,
        "category": "todo",
    }
    return await _add(session, TaskStatus(**(defaults | values)))


async def next_task_number(session: AsyncSession, project: Project) -> int:
    """Reserve the project's next task number the way the app does (P-6):
    one atomic UPDATE ... RETURNING, safe under concurrent inserts."""
    number = await session.scalar(
        update(Project)
        .where(Project.id == project.id)
        .values(next_task_number=Project.next_task_number + 1)
        .returning(Project.next_task_number - 1)
    )
    if number is None:
        raise ValueError(f"Project {project.id} does not exist")
    return number


async def make_task(
    session: AsyncSession,
    *,
    project: Project | None = None,
    space: Space | None = None,
    status: TaskStatus | None = None,
    creator: User | None = None,
    **values: Any,
) -> Task:
    project = project or await make_project(session)
    space = space or await make_space(session, project=project)
    status = status or await make_task_status(session, project=project)
    if creator is None:
        organization = await session.get(Organization, project.organization_id)
        creator = await make_user(session, organization=organization)
    n = _next()
    defaults = {
        "project_id": project.id,
        "space_id": space.id,
        "status_id": status.id,
        "created_by": creator.id,
        "title": f"Task {n}",
    }
    if "number" not in values:
        defaults["number"] = await next_task_number(session, project)
    return await _add(session, Task(**(defaults | values)))


async def make_task_comment(
    session: AsyncSession,
    *,
    task: Task | None = None,
    author: User | None = None,
    **values: Any,
) -> TaskComment:
    task = task or await make_task(session)
    if author is None:
        author = await session.get(User, task.created_by)
    n = _next()
    defaults = {"task_id": task.id, "author_id": author.id, "body": f"Comment {n}"}
    return await _add(session, TaskComment(**(defaults | values)))


async def make_team(
    session: AsyncSession, *, project: Project | None = None, **values: Any
) -> Team:
    project = project or await make_project(session)
    n = _next()
    defaults = {"project_id": project.id, "name": f"Team {n}"}
    return await _add(session, Team(**(defaults | values)))


async def make_role(
    session: AsyncSession, *, organization: Organization | None = None, **values: Any
) -> Role:
    organization = organization or await make_organization(session)
    n = _next()
    defaults = {"organization_id": organization.id, "name": f"Role {n}"}
    return await _add(session, Role(**(defaults | values)))
