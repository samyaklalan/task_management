"""M8: MEM-1 list, MEM-2 add, MEM-3 change role, MEM-4 remove (A-7)."""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import anyio
import httpx
import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.session import get_session
from app.main import create_app
from app.models import Organization, Project, Role, Task, TeamMember, User, UserRole
from tests.api.project_helpers import REGISTER, bearer, member, new_project, outsider
from tests.factories import (
    make_organization,
    make_project,
    make_role,
    make_task,
    make_team,
    make_user,
)
from tests.utils.query_counter import count_queries


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post("/auth/register", json=REGISTER)
    assert response.status_code == 201
    return {"headers": bearer(response.json()["access_token"])}


@pytest.fixture
async def acme(db_session: AsyncSession, admin: dict[str, Any]) -> Organization:
    return await db_session.scalar(select(Organization).where(Organization.slug == "acme"))


@pytest.fixture
async def web(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any]
) -> Project:
    created = await new_project(client, admin["headers"])
    return await db_session.scalar(select(Project).where(Project.public_id == created["public_id"]))


async def person(
    session: AsyncSession,
    organization: Organization,
    name: str,
    *,
    org_role: Role | None = None,
    project_role: tuple[Project, Role] | None = None,
    **values: Any,
) -> User:
    user = await make_user(session, organization=organization, name=name, **values)
    if org_role:
        session.add(UserRole(user_id=user.id, role_id=org_role.id, project_id=None))
    if project_role:
        project, role = project_role
        session.add(UserRole(user_id=user.id, role_id=role.id, project_id=project.id))
    await session.flush()
    return user


def url(project: Project, user_id: int | None = None) -> str:
    base = f"/projects/{project.public_id}/members"
    return base if user_id is None else f"{base}/{user_id}"


async def listed(client: httpx.AsyncClient, headers: dict, project: Project, query: str = ""):
    response = await client.get(url(project) + query, headers=headers)
    assert response.status_code == 200, response.text
    return [(i["user"]["name"], i["role"]["name"], i["scope"]) for i in response.json()["items"]]


async def archive(session: AsyncSession, project: Project) -> None:
    await session.execute(update(Project).where(Project.id == project.id).values(status="archived"))


# ── MEM-1 GET /projects/{public_id}/members ───────────────────────────────────


async def test_list_shows_org_wide_and_project_members_once(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    developer = await make_role(db_session, organization=acme, name="Developer")
    tester = await make_role(db_session, organization=acme, name="Tester")
    viewer = await make_role(db_session, organization=acme, name="Viewer")
    other_project = await make_project(db_session, organization=acme)
    await person(db_session, acme, "ravi Kumar", project_role=(web, developer))
    # Org-wide AND project role: listed once, with the project role (changeable here).
    await person(db_session, acme, "Meera", org_role=viewer, project_role=(web, tester))
    await person(db_session, acme, "Zed", project_role=(web, developer), is_active=False)
    await person(db_session, acme, "Nina", project_role=(other_project, developer))
    await person(db_session, acme, "No Role")
    foreign_org = await make_organization(db_session)
    await person(
        db_session,
        foreign_org,
        "Outsider",
        org_role=await make_role(db_session, organization=foreign_org),
    )

    response = await client.get(url(web), headers=admin["headers"])

    assert [
        (i["user"]["name"], i["role"]["name"], i["scope"]) for i in response.json()["items"]
    ] == [
        ("Asha Rao", "Admin", "organization"),
        ("Meera", "Tester", "project"),
        ("ravi Kumar", "Developer", "project"),
    ]
    item = response.json()["items"][0]
    assert set(item) == {"user", "role", "scope"}
    assert set(item["user"]) == {"id", "name"}  # no email (only USR-3 lists emails)
    assert response.json()["pagination"]["total_items"] == 3


async def test_list_search_and_paging(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    developer = await make_role(db_session, organization=acme, name="Developer")
    for name in ("Ravi Kumar", "Ravina Shah", "Kumar Sanu"):
        await person(db_session, acme, name, project_role=(web, developer))
    headers = admin["headers"]

    assert [n for n, _, _ in await listed(client, headers, web, "?q=RAVI")] == [
        "Ravi Kumar",
        "Ravina Shah",
    ]
    assert [n for n, _, _ in await listed(client, headers, web, "?q=kumar")] == [
        "Kumar Sanu",
        "Ravi Kumar",
    ]
    assert [n for n, _, _ in await listed(client, headers, web, "?page=2&page_size=3")] == [
        "Ravina Shah"
    ]
    too_short = await client.get(url(web) + "?q=ra", headers=headers)
    assert too_short.status_code == 422
    assert too_short.json()["error"]["code"] == "search_too_short"


async def test_list_query_count_is_constant(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    developer = await make_role(db_session, organization=acme)
    for n in range(8):
        await person(db_session, acme, f"Dev {n}", project_role=(web, developer))

    with count_queries(engine) as queries:
        response = await client.get(url(web), headers=admin["headers"])

    assert len(response.json()["items"]) == 9
    # user + project + permissions + COUNT + page (users and roles joined in)
    assert queries.count == 5, str(queries)


async def test_list_needs_members_view_or_tasks_view(
    client: httpx.AsyncClient, db_session: AsyncSession, acme: Organization, web: Project
) -> None:
    task_editor = await member(db_session, acme, {"tasks": "view,edit"}, web)
    member_viewer = await member(db_session, acme, {"members": "view"}, web)
    project_viewer = await member(db_session, acme, {"projects": "view"}, web)
    nobody = await outsider(db_session, acme)

    assert (await client.get(url(web), headers=task_editor)).status_code == 200  # the picker
    assert (await client.get(url(web), headers=member_viewer)).status_code == 200
    assert (await client.get(url(web), headers=project_viewer)).status_code == 403
    assert (await client.get(url(web), headers=nobody)).status_code == 404

    await archive(db_session, web)
    assert (await client.get(url(web), headers=task_editor)).status_code == 200  # reads still work


# ── MEM-2 POST /projects/{public_id}/members ──────────────────────────────────


async def test_add_member(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    developer = await make_role(db_session, organization=acme, name="Developer")
    ravi = await person(db_session, acme, "Ravi Kumar")
    body = {"user_id": ravi.id, "role_id": developer.id}

    response = await client.post(url(web), headers=admin["headers"], json=body)
    again = await client.post(url(web), headers=admin["headers"], json=body)

    assert response.status_code == 201
    assert response.json() == {
        "user": {"id": body["user_id"], "name": "Ravi Kumar"},
        "role": {"id": body["role_id"], "name": "Developer"},
        "scope": "project",
    }
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_member"
    assert ("Ravi Kumar", "Developer", "project") in await listed(client, admin["headers"], web)


async def test_org_wide_user_can_also_get_a_project_role(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    viewer = await make_role(db_session, organization=acme, name="Viewer")
    lead = await make_role(db_session, organization=acme, name="Lead")
    meera = await person(db_session, acme, "Meera", org_role=viewer)
    assert ("Meera", "Viewer", "organization") in await listed(client, admin["headers"], web)

    response = await client.post(
        url(web), headers=admin["headers"], json={"user_id": meera.id, "role_id": lead.id}
    )

    assert response.status_code == 201
    members = await listed(client, admin["headers"], web)
    assert ("Meera", "Lead", "project") in members
    assert ("Meera", "Viewer", "organization") not in members  # still listed only once


async def test_add_member_invalid_references(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    role = await make_role(db_session, organization=acme)
    user = await person(db_session, acme, "Ravi")
    inactive = await person(db_session, acme, "Gone", is_active=False)
    foreign_org = await make_organization(db_session)
    foreign_user = await make_user(db_session, organization=foreign_org)
    foreign_role = await make_role(db_session, organization=foreign_org)

    cases = [
        ({"user_id": foreign_user.id, "role_id": role.id}, "user_id"),
        ({"user_id": 999_999_999, "role_id": role.id}, "user_id"),
        ({"user_id": inactive.id, "role_id": role.id}, "user_id"),
        ({"user_id": user.id, "role_id": foreign_role.id}, "role_id"),
        ({"user_id": user.id, "role_id": 999_999_999}, "role_id"),
    ]
    for body, field in cases:
        response = await client.post(url(web), headers=admin["headers"], json=body)
        assert response.status_code == 422, body
        assert response.json()["error"]["code"] == "invalid_reference"
        assert response.json()["error"]["details"][0]["field"] == field

    for body in (
        {"user_id": user.id},
        {"user_id": 0, "role_id": role.id},
        {"user_id": user.id, "role_id": role.id, "scope": "x"},
    ):
        response = await client.post(url(web), headers=admin["headers"], json=body)
        assert response.json()["error"]["code"] == "validation_error", body


async def test_add_member_permissions_and_archived(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    role = await make_role(db_session, organization=acme)
    ravi = await person(db_session, acme, "Ravi")
    body = {"user_id": ravi.id, "role_id": role.id}
    viewer = await member(db_session, acme, {"members": "view,edit,delete"}, web)
    creator = await member(db_session, acme, {"members": "create"}, web)

    assert (await client.post(url(web), headers=viewer, json=body)).status_code == 403
    await archive(db_session, web)
    archived = await client.post(url(web), headers=admin["headers"], json=body)
    assert archived.status_code == 409
    assert archived.json()["error"]["code"] == "project_archived"
    await db_session.execute(update(Project).where(Project.id == web.id).values(status="active"))
    assert (await client.post(url(web), headers=creator, json=body)).status_code == 201


# ── MEM-3 PATCH /projects/{public_id}/members/{user_id} ───────────────────────


async def test_change_member_role(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    developer = await make_role(db_session, organization=acme, name="Developer")
    lead = await make_role(db_session, organization=acme, name="Lead")
    ravi = await person(db_session, acme, "Ravi Kumar", project_role=(web, developer))

    response = await client.patch(
        url(web, ravi.id), headers=admin["headers"], json={"role_id": lead.id}
    )

    assert response.status_code == 200
    assert response.json() == {
        "user": {"id": ravi.id, "name": "Ravi Kumar"},
        "role": {"id": lead.id, "name": "Lead"},
        "scope": "project",
    }
    assert ("Ravi Kumar", "Lead", "project") in await listed(client, admin["headers"], web)


async def test_change_role_only_for_project_members(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    role = await make_role(db_session, organization=acme)
    org_wide = await person(db_session, acme, "Org Wide", org_role=role)
    elsewhere = await person(
        db_session,
        acme,
        "Elsewhere",
        project_role=(await make_project(db_session, organization=acme), role),
    )
    foreign_org = await make_organization(db_session)
    foreign_user = await make_user(db_session, organization=foreign_org)
    foreign_role = await make_role(db_session, organization=foreign_org)
    member_user = await person(db_session, acme, "Member", project_role=(web, role))

    for user_id in (org_wide.id, elsewhere.id, foreign_user.id, 999_999_999):
        response = await client.patch(
            url(web, user_id), headers=admin["headers"], json={"role_id": role.id}
        )
        assert response.status_code == 404, user_id
    # The org-wide role is untouched (managed with USR-6, not here).
    assert ("Org Wide", role.name, "organization") in await listed(client, admin["headers"], web)

    bad_role = await client.patch(
        url(web, member_user.id), headers=admin["headers"], json={"role_id": foreign_role.id}
    )
    assert bad_role.status_code == 422
    assert bad_role.json()["error"]["details"][0]["field"] == "role_id"


async def test_change_role_permissions_and_archived(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    role = await make_role(db_session, organization=acme)
    ravi = await person(db_session, acme, "Ravi", project_role=(web, role))
    adder = await member(db_session, acme, {"members": "view,create,delete"}, web)
    editor = await member(db_session, acme, {"members": "edit"}, web)

    assert (
        await client.patch(url(web, ravi.id), headers=adder, json={"role_id": role.id})
    ).status_code == 403
    assert (
        await client.patch(url(web, ravi.id), headers=editor, json={"role_id": role.id})
    ).status_code == 200
    await archive(db_session, web)
    assert (
        await client.patch(url(web, ravi.id), headers=admin["headers"], json={"role_id": role.id})
    ).status_code == 409


# ── MEM-4 DELETE /projects/{public_id}/members/{user_id} ──────────────────────


async def test_remove_member_unassigns_tasks_and_leaves_teams(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    role = await make_role(db_session, organization=acme)
    other = await make_project(db_session, organization=acme)
    ravi = await person(db_session, acme, "Ravi", project_role=(web, role))
    db_session.add(UserRole(user_id=ravi.id, role_id=role.id, project_id=other.id))
    here_tasks = [await make_task(db_session, project=web, assignee_id=ravi.id) for _ in range(2)]
    there_task = await make_task(db_session, project=other, assignee_id=ravi.id)
    here_team, there_team = (
        await make_team(db_session, project=web),
        await make_team(db_session, project=other),
    )
    db_session.add_all(
        [
            TeamMember(team_id=here_team.id, user_id=ravi.id),
            TeamMember(team_id=there_team.id, user_id=ravi.id),
        ]
    )
    await db_session.flush()
    before: dict[int, datetime] = {t.id: t.updated_at for t in here_tasks}

    response = await client.delete(url(web, ravi.id), headers=admin["headers"])

    assert response.status_code == 204
    assert response.content == b""
    tasks = {
        t.id: t
        for t in await db_session.scalars(
            select(Task)
            .where(Task.assignee_id.is_(None) | (Task.assignee_id == ravi.id))
            .execution_options(populate_existing=True)
        )
    }
    for task_id, updated_at in before.items():
        assert tasks[task_id].assignee_id is None
        assert tasks[task_id].updated_at > updated_at  # editors of it get a fair stale_update
    assert tasks[there_task.id].assignee_id == ravi.id  # other projects untouched
    teams = set(
        await db_session.scalars(select(TeamMember.team_id).where(TeamMember.user_id == ravi.id))
    )
    assert teams == {there_team.id}
    roles = set(
        await db_session.scalars(select(UserRole.project_id).where(UserRole.user_id == ravi.id))
    )
    assert roles == {other.id}
    assert "Ravi" not in [n for n, _, _ in await listed(client, admin["headers"], web)]

    again = await client.delete(url(web, ravi.id), headers=admin["headers"])
    assert again.status_code == 404


async def test_remove_only_touches_project_roles(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    viewer = await make_role(db_session, organization=acme, name="Viewer")
    lead = await make_role(db_session, organization=acme, name="Lead")
    org_only = await person(db_session, acme, "Org Only", org_role=viewer)
    both = await person(db_session, acme, "Both", org_role=viewer, project_role=(web, lead))

    assert (await client.delete(url(web, org_only.id), headers=admin["headers"])).status_code == 404
    assert (await client.delete(url(web, both.id), headers=admin["headers"])).status_code == 204

    members = await listed(client, admin["headers"], web)
    # The org-wide role still gives access: listed again, now with that role.
    assert ("Org Only", "Viewer", "organization") in members
    assert ("Both", "Viewer", "organization") in members


async def test_remove_member_permissions_and_archived(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    role = await make_role(db_session, organization=acme)
    ravi = await person(db_session, acme, "Ravi", project_role=(web, role))
    editor = await member(db_session, acme, {"members": "view,create,edit"}, web)
    remover = await member(db_session, acme, {"members": "delete"}, web)
    nobody = await outsider(db_session, acme)

    assert (await client.delete(url(web, ravi.id), headers=nobody)).status_code == 404
    assert (await client.delete(url(web, ravi.id), headers=editor)).status_code == 403
    await archive(db_session, web)
    assert (await client.delete(url(web, ravi.id), headers=remover)).status_code == 409
    await db_session.execute(update(Project).where(Project.id == web.id).values(status="active"))
    assert (await client.delete(url(web, ravi.id), headers=remover)).status_code == 204


# ── Concurrency: real, separate transactions ─────────────────────────────────

RACE_SLUG = "race-members"


@pytest.fixture
async def real_client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    application = create_app()

    async def separate_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    application.dependency_overrides[get_session] = separate_session
    transport = httpx.ASGITransport(app=application)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1") as http:
            yield http
    finally:
        async with AsyncSession(engine) as cleanup:
            org_ids = select(Organization.id).where(Organization.slug == RACE_SLUG)
            # user_roles, spaces and statuses go with their project / user (CASCADE).
            await cleanup.execute(delete(Project).where(Project.organization_id.in_(org_ids)))
            await cleanup.execute(delete(User).where(User.organization_id.in_(org_ids)))
            await cleanup.execute(delete(Role).where(Role.organization_id.in_(org_ids)))
            await cleanup.execute(delete(Organization).where(Organization.slug == RACE_SLUG))
            await cleanup.commit()


async def test_two_simultaneous_adds_one_wins(
    real_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    signed_up = await real_client.post(
        "/auth/register", json=REGISTER | {"organization_slug": RACE_SLUG}
    )
    headers = bearer(signed_up.json()["access_token"])
    project = await new_project(real_client, headers)
    async with AsyncSession(engine, expire_on_commit=False) as setup:
        organization = await setup.scalar(
            select(Organization).where(Organization.slug == RACE_SLUG)
        )
        ravi = await make_user(setup, organization=organization, name="Ravi")
        developer = await make_role(setup, organization=organization, name="Developer")
        lead = await make_role(setup, organization=organization, name="Lead")
        await setup.commit()
    statuses: list[int] = []

    async def add(role_id: int) -> None:
        response = await real_client.post(
            f"/projects/{project['public_id']}/members",
            headers=headers,
            json={"user_id": ravi.id, "role_id": role_id},
        )
        statuses.append(response.status_code)

    async with anyio.create_task_group() as group:
        group.start_soon(add, developer.id)
        group.start_soon(add, lead.id)

    assert sorted(statuses) == [201, 409]
    async with AsyncSession(engine) as check:
        rows = await check.scalar(
            select(func.count()).select_from(UserRole).where(UserRole.user_id == ravi.id)
        )
    assert rows == 1  # never two roles in one project
