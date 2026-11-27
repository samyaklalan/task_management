"""Shared helpers for the project API tests (M7 onwards)."""

from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.models import Organization, Project, RolePermission, TaskStatus, UserRole
from tests.factories import make_role, make_user

REGISTER = {
    "organization_name": "Acme Corp",
    "organization_slug": "acme",
    "name": "Asha Rao",
    "email": "asha@acme.com",
    "password": "correct-horse-battery",
}
ON = {"view": True, "create": True, "edit": True, "delete": True}
OFF = {"view": False, "create": False, "edit": False, "delete": False}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def member(
    session: AsyncSession,
    organization: Organization,
    grants: dict[str, str],
    project: Project | None = None,
) -> dict[str, str]:
    """A new user with a role (org-wide, or in ``project``) allowing ``grants``,
    e.g. {"tasks": "view,create"}. Returns their auth header."""
    user = await make_user(session, organization=organization)
    role = await make_role(session, organization=organization)
    for feature, actions in grants.items():
        flags = {f"{action}_perm": True for action in actions.split(",") if action}
        session.add(RolePermission(role_id=role.id, feature=feature, **flags))
    session.add(
        UserRole(user_id=user.id, role_id=role.id, project_id=project.id if project else None)
    )
    await session.flush()
    return bearer(create_access_token(user.id, organization.id))


async def outsider(session: AsyncSession, organization: Organization) -> dict[str, str]:
    """A user of the organization with no role at all."""
    user = await make_user(session, organization=organization)
    return bearer(create_access_token(user.id, organization.id))


async def new_project(client: httpx.AsyncClient, headers: dict, **body: Any) -> dict:
    payload = {"name": "Website Revamp", "task_prefix": "WEB"} | body
    response = await client.post("/projects", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def project_user(
    session: AsyncSession, organization: Organization, project: Project, name: str, **values: Any
) -> int:
    """A user with a role in ``project`` (so they can be assigned there).
    Returns the id: a plain int survives a rollback in the shared test session."""
    user = await make_user(session, organization=organization, name=name, **values)
    role = await make_role(session, organization=organization)
    session.add(UserRole(user_id=user.id, role_id=role.id, project_id=project.id))
    await session.flush()
    return user.id


def tasks_url(project: Project, tail: str = "") -> str:
    return f"/projects/{project.public_id}/tasks{tail}"


async def new_task(
    client: httpx.AsyncClient, headers: dict, project: Project, **body: Any
) -> dict:
    response = await client.post(
        tasks_url(project), headers=headers, json={"title": "A task"} | body
    )
    assert response.status_code == 201, response.text
    return response.json()


async def status_ids(session: AsyncSession, project: Project) -> dict[str, int]:
    """``{status name: id}`` for the project."""
    rows = await session.execute(
        select(TaskStatus.name, TaskStatus.id).where(TaskStatus.project_id == project.id)
    )
    return dict(rows.all())
