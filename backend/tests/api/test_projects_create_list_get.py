"""M7a: PRJ-1 create, PRJ-2 list, PRJ-3 details, and the project guard."""

import re
from typing import Annotated, Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.deps import ProjectContext, require_project_permission
from app.core.permissions import Action, Feature
from app.core.security import create_access_token
from app.models import Organization, Project, Space, TaskStatus, User
from tests.api.project_helpers import (
    OFF,
    ON,
    REGISTER,
    bearer,
    member,
    new_project,
)
from tests.factories import (
    make_organization,
    make_project,
    make_task,
    make_task_status,
    make_user,
)
from tests.utils.query_counter import count_queries

PROJECT_FEATURES = ["projects", "members", "teams", "spaces", "task_statuses", "tasks", "comments"]


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    """The normal app plus two test routes that use the project guard."""

    async def write_tasks(
        project: Annotated[ProjectContext, require_project_permission(Feature.TASKS, Action.EDIT)],
    ) -> dict:
        return {"project": project.project.public_id}

    async def read_tasks(
        project: Annotated[ProjectContext, require_project_permission(Feature.TASKS, Action.VIEW)],
    ) -> dict:
        return {"project": project.project.public_id}

    app.add_api_route("/api/v1/test/projects/{public_id}/write", write_tasks, methods=["POST"])
    app.add_api_route("/api/v1/test/projects/{public_id}/read", read_tasks)
    return app


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post("/auth/register", json=REGISTER)
    assert response.status_code == 201
    return {"headers": bearer(response.json()["access_token"])}


@pytest.fixture
async def acme(db_session: AsyncSession, admin: dict[str, Any]) -> Organization:
    return await db_session.scalar(select(Organization).where(Organization.slug == "acme"))


# ── PRJ-1 POST /projects ──────────────────────────────────────────────────────


async def test_create_project_with_defaults(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any]
) -> None:
    body = await new_project(
        client,
        admin["headers"],
        name="  Website Revamp ",
        task_prefix=" web ",
        description="New marketing site",
        start_date="2026-12-01",
        end_date="2027-03-31",
    )

    assert re.fullmatch(r"[a-z0-9]{12}", body["public_id"])
    assert (body["name"], body["task_prefix"], body["status"]) == (
        "Website Revamp",
        "WEB",
        "active",
    )
    assert (body["start_date"], body["end_date"]) == ("2026-12-01", "2027-03-31")
    assert body["task_counts"] == {"todo": 0, "in_progress": 0, "done": 0, "total": 0}
    assert list(body["my_permissions"]) == PROJECT_FEATURES
    assert all(flags == ON for flags in body["my_permissions"].values())  # the Admin

    project = await db_session.scalar(select(Project).where(Project.public_id == body["public_id"]))
    assert project.next_task_number == 1
    spaces = (
        await db_session.execute(
            select(Space.name, Space.position).where(Space.project_id == project.id)
        )
    ).all()
    assert spaces == [("General", 0)]
    statuses = (
        await db_session.execute(
            select(TaskStatus.name, TaskStatus.category, TaskStatus.color, TaskStatus.position)
            .where(TaskStatus.project_id == project.id)
            .order_by(TaskStatus.position)
        )
    ).all()
    assert statuses == [
        ("To Do", "todo", "#64748B", 0),
        ("In Progress", "in_progress", "#3B82F6", 1),
        ("Done", "done", "#22C55E", 2),
    ]


async def test_task_prefix_must_be_unique_in_the_organization(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any]
) -> None:
    await new_project(client, admin["headers"], task_prefix="WEB")
    other_org = await make_organization(db_session)
    await make_project(db_session, organization=other_org, task_prefix="APP")

    taken = await client.post(
        "/projects", headers=admin["headers"], json={"name": "Again", "task_prefix": "web"}
    )
    elsewhere = await client.post(
        "/projects", headers=admin["headers"], json={"name": "App", "task_prefix": "APP"}
    )

    assert taken.status_code == 409
    assert taken.json()["error"]["details"] == [
        {
            "field": "task_prefix",
            "message": "This task prefix is already used in your organization.",
        }
    ]
    assert elsewhere.status_code == 201  # "APP" is only taken in another organization
    spaces = await db_session.scalar(select(func.count()).select_from(Space))
    assert spaces == 2  # the refused project left nothing behind


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"task_prefix": "W"}, "task_prefix"),
        ({"task_prefix": "1WEB"}, "task_prefix"),
        ({"task_prefix": "WEB-1"}, "task_prefix"),
        ({"task_prefix": "ABCDEFGHIJK"}, "task_prefix"),
        ({"name": "  "}, "name"),
        ({"description": "x" * 2001}, "description"),
        ({"status": "archived"}, "status"),  # always starts active
        ({"start_date": "2027-01-02", "end_date": "2027-01-01"}, None),
    ],
)
async def test_invalid_project_body(
    client: httpx.AsyncClient, admin: dict[str, Any], changes: dict, field: str | None
) -> None:
    payload = {"name": "Website", "task_prefix": "WEB"} | changes

    response = await client.post("/projects", headers=admin["headers"], json=payload)

    assert response.status_code == 422
    if field:
        assert [d["field"] for d in response.json()["error"]["details"]] == [field]
    else:
        assert "end_date can't be before start_date" in response.text


async def test_creating_a_project_needs_the_org_wide_permission(
    client: httpx.AsyncClient, db_session: AsyncSession, acme: Organization
) -> None:
    project = await make_project(db_session, organization=acme)
    # projects.create in a PROJECT role doesn't count (rule 1)...
    project_role = await member(db_session, acme, {"projects": "view,create"}, project)
    # ...in the org-wide role it does.
    org_role = await member(db_session, acme, {"projects": "create"})

    refused = await client.post(
        "/projects", headers=project_role, json={"name": "X", "task_prefix": "XX"}
    )
    allowed = await client.post(
        "/projects", headers=org_role, json={"name": "Y", "task_prefix": "YY"}
    )

    assert refused.status_code == 403
    assert allowed.status_code == 201


# ── PRJ-2 GET /projects ───────────────────────────────────────────────────────


@pytest.fixture
async def portfolio(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], acme: Organization
) -> dict[str, dict]:
    """Four Acme projects (one archived, one on hold), plus another
    organization's project that must never show up."""
    created = {
        "web": await new_project(
            client, admin["headers"], name="Website", task_prefix="WEB", end_date="2027-01-31"
        ),
        "app": await new_project(client, admin["headers"], name="mobile App", task_prefix="APP"),
        "ops": await new_project(
            client, admin["headers"], name="Ops 50%_done", task_prefix="OPS", end_date="2026-12-31"
        ),
        "old": await new_project(client, admin["headers"], name="Old site", task_prefix="OLD"),
    }
    await db_session.execute(
        update(Project)
        .where(Project.public_id == created["old"]["public_id"])
        .values(status="archived")
    )
    await db_session.execute(
        update(Project)
        .where(Project.public_id == created["ops"]["public_id"])
        .values(status="on_hold")
    )
    await make_project(
        db_session, organization=await make_organization(db_session), name="Website X"
    )
    return created


async def names(client: httpx.AsyncClient, headers: dict, query: str = "") -> list[str]:
    response = await client.get(f"/projects{query}", headers=headers)
    assert response.status_code == 200, response.text
    return [item["name"] for item in response.json()["items"]]


async def test_list_hides_archived_and_other_organizations(
    client: httpx.AsyncClient, admin: dict[str, Any], portfolio: dict[str, dict]
) -> None:
    response = await client.get("/projects?sort=name&order=asc", headers=admin["headers"])

    body = response.json()
    assert [item["name"] for item in body["items"]] == ["mobile App", "Ops 50%_done", "Website"]
    assert set(body["items"][0]) == {
        "public_id",
        "name",
        "status",
        "task_prefix",
        "start_date",
        "end_date",
        "updated_at",
    }
    assert body["pagination"]["total_items"] == 3


async def test_list_filters_search_and_sorting(
    client: httpx.AsyncClient, admin: dict[str, Any], portfolio: dict[str, dict]
) -> None:
    headers = admin["headers"]
    assert await names(client, headers, "?status=archived") == ["Old site"]
    assert await names(client, headers, "?status=on_hold") == ["Ops 50%_done"]
    assert await names(client, headers, "?q=SITE") == ["Website"]  # archived "Old site" hidden
    assert await names(client, headers, "?q=SITE&status=archived") == ["Old site"]
    assert await names(client, headers, "?q=50%25_") == ["Ops 50%_done"]
    # end_date: projects without one come last, in both directions.
    assert await names(client, headers, "?sort=end_date&order=asc") == [
        "Ops 50%_done",
        "Website",
        "mobile App",
    ]
    assert await names(client, headers, "?sort=end_date&order=desc") == [
        "Website",
        "Ops 50%_done",
        "mobile App",
    ]
    page_2 = await client.get("/projects?sort=name&order=asc&page=2&page_size=2", headers=headers)
    assert [item["name"] for item in page_2.json()["items"]] == ["Website"]


async def test_members_see_only_their_projects(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    acme: Organization,
    portfolio: dict[str, dict],
) -> None:
    web = await db_session.scalar(
        select(Project).where(Project.public_id == portfolio["web"]["public_id"])
    )
    app = await db_session.scalar(
        select(Project).where(Project.public_id == portfolio["app"]["public_id"])
    )
    web_member = await member(db_session, acme, {"projects": "view", "tasks": "view"}, web)
    no_project_view = await member(db_session, acme, {"tasks": "view"}, app)
    org_role_without_view = await member(db_session, acme, {"users": "view"})
    nobody = bearer(
        create_access_token((await make_user(db_session, organization=acme)).id, acme.id)
    )

    assert await names(client, web_member) == ["Website"]
    assert await names(client, no_project_view) == []
    assert await names(client, org_role_without_view) == []
    assert await names(client, nobody) == []  # an empty list, not a 403


@pytest.mark.parametrize("query", ["?status=deleted", "?sort=description", "?order=up", "?q=ab"])
async def test_invalid_list_parameters(
    client: httpx.AsyncClient, admin: dict[str, Any], query: str
) -> None:
    assert (await client.get(f"/projects{query}", headers=admin["headers"])).status_code == 422


async def test_list_query_count_is_constant(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    portfolio: dict[str, dict],
) -> None:
    web = await db_session.scalar(
        select(Project).where(Project.public_id == portfolio["web"]["public_id"])
    )
    web_member = await member(db_session, acme, {"projects": "view"}, web)

    for headers in (admin["headers"], web_member):
        with count_queries(engine) as queries:
            await client.get("/projects", headers=headers)
        # user + org permissions + COUNT + page (the visibility rule is a subquery)
        assert queries.count == 4, str(queries)


# ── PRJ-3 GET /projects/{public_id} ───────────────────────────────────────────


async def test_project_details_with_task_counts(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    db_session: AsyncSession,
    admin: dict[str, Any],
) -> None:
    created = await new_project(client, admin["headers"])
    project = await db_session.scalar(
        select(Project).where(Project.public_id == created["public_id"])
    )
    statuses = {
        s.category: s
        for s in await db_session.scalars(
            select(TaskStatus).where(TaskStatus.project_id == project.id)
        )
    }
    review = await make_task_status(
        db_session, project=project, name="Review", category="in_progress", position=3
    )
    for status, how_many in (
        (statuses["todo"], 3),
        (statuses["in_progress"], 1),
        (review, 2),
        (statuses["done"], 4),
    ):
        for _ in range(how_many):
            await make_task(db_session, project=project, status=status)

    with count_queries(engine) as queries:
        response = await client.get(f"/projects/{created['public_id']}", headers=admin["headers"])

    assert response.status_code == 200
    body = response.json()
    assert body["task_counts"] == {"todo": 3, "in_progress": 3, "done": 4, "total": 10}
    assert body["description"] is None
    # user + project + permissions + one GROUP BY for the counts
    assert queries.count == 4, str(queries)


async def test_my_permissions_combine_org_and_project_roles(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], acme: Organization
) -> None:
    created = await new_project(client, admin["headers"])
    project = await db_session.scalar(
        select(Project).where(Project.public_id == created["public_id"])
    )
    headers = await member(db_session, acme, {"projects": "view", "tasks": "view,create"}, project)

    body = (await client.get(f"/projects/{created['public_id']}", headers=headers)).json()

    assert body["my_permissions"]["tasks"] == OFF | {"view": True, "create": True}
    assert body["my_permissions"]["projects"] == OFF | {"view": True}
    assert body["my_permissions"]["members"] == OFF


async def test_projects_you_cannot_see_are_not_found(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], acme: Organization
) -> None:
    created = await new_project(client, admin["headers"])
    project = await db_session.scalar(
        select(Project).where(Project.public_id == created["public_id"])
    )
    foreign = await make_project(db_session, organization=await make_organization(db_session))
    no_role = bearer(
        create_access_token((await make_user(db_session, organization=acme)).id, acme.id)
    )
    role_without_view = await member(db_session, acme, {"tasks": "view"}, project)

    assert (
        await client.get(f"/projects/{foreign.public_id}", headers=admin["headers"])
    ).status_code == 404
    assert (
        await client.get("/projects/nosuchproject", headers=admin["headers"])
    ).status_code == 404
    assert (
        await client.get(f"/projects/{created['public_id']}", headers=no_role)
    ).status_code == 404
    assert (
        await client.get(f"/projects/{created['public_id']}", headers=role_without_view)
    ).status_code == 404


async def test_too_long_public_id_is_a_validation_error(
    client: httpx.AsyncClient, admin: dict[str, Any]
) -> None:
    response = await client.get(f"/projects/{'x' * 17}", headers=admin["headers"])

    assert response.status_code == 422


# ── The project guard (used by every project-scoped module) ───────────────────


async def test_project_guard_order_404_then_403_then_archived(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], acme: Organization
) -> None:
    created = await new_project(client, admin["headers"])
    public_id = created["public_id"]
    project = await db_session.scalar(select(Project).where(Project.public_id == public_id))
    reader = await member(db_session, acme, {"tasks": "view"}, project)
    outsider = bearer(
        create_access_token((await make_user(db_session, organization=acme)).id, acme.id)
    )

    assert (
        await client.post(f"/test/projects/{public_id}/write", headers=outsider)
    ).status_code == 404
    assert (
        await client.post(f"/test/projects/{public_id}/write", headers=reader)
    ).status_code == 403
    assert (await client.get(f"/test/projects/{public_id}/read", headers=reader)).status_code == 200
    assert (
        await client.post(f"/test/projects/{public_id}/write", headers=admin["headers"])
    ).status_code == 200

    await db_session.execute(
        update(Project).where(Project.id == project.id).values(status="archived")
    )

    archived_write = await client.post(
        f"/test/projects/{public_id}/write", headers=admin["headers"]
    )
    archived_read = await client.get(f"/test/projects/{public_id}/read", headers=reader)
    assert archived_write.status_code == 409
    assert archived_write.json()["error"]["code"] == "project_archived"
    assert archived_read.status_code == 200  # reads still work (A-9)
    assert (
        await client.post(f"/test/projects/{public_id}/write", headers=reader)
    ).status_code == 403
    assert (await client.get(f"/projects/{public_id}", headers=admin["headers"])).json()[
        "status"
    ] == "archived"


async def test_deactivated_user_cannot_open_projects(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any]
) -> None:
    created = await new_project(client, admin["headers"])
    await db_session.execute(update(User).values(is_active=False))

    response = await client.get(f"/projects/{created['public_id']}", headers=admin["headers"])

    assert response.status_code == 401
