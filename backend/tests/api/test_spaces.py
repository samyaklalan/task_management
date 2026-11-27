"""M10: SPC-1 to SPC-5 (no delete, S-5)."""

from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.session import get_session
from app.main import create_app
from app.models import Organization, Project, Role, Space, User
from app.repositories import space as space_repository
from tests.api.project_helpers import REGISTER, bearer, member, new_project, outsider
from tests.factories import make_project, make_space, make_task
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
    """A project made through the API, so it has its "General" space at 0."""
    created = await new_project(client, admin["headers"])
    return await db_session.scalar(select(Project).where(Project.public_id == created["public_id"]))


def url(project: Project, tail: str = "") -> str:
    return f"/projects/{project.public_id}/spaces{tail}"


async def add(client: httpx.AsyncClient, headers: dict, project: Project, name: str, **body):
    response = await client.post(url(project), headers=headers, json={"name": name} | body)
    assert response.status_code == 201, response.text
    return response.json()


async def sidebar(client: httpx.AsyncClient, headers: dict, project: Project) -> list:
    response = await client.get(url(project), headers=headers)
    assert response.status_code == 200, response.text
    return [(s["name"], s["position"]) for s in response.json()["items"]]


# ── SPC-1 / SPC-2 / SPC-3 ─────────────────────────────────────────────────────


async def test_new_spaces_go_to_the_end(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project
) -> None:
    headers = admin["headers"]

    frontend = await add(client, headers, web, " Frontend ", description="UI work")
    qa = await add(client, headers, web, "QA")

    assert (frontend["name"], frontend["description"], frontend["position"]) == (
        "Frontend",
        "UI work",
        1,
    )
    assert frontend["task_count"] == 0
    assert set(frontend) == {
        "id",
        "name",
        "description",
        "position",
        "task_count",
        "created_at",
        "updated_at",
    }
    assert qa["position"] == 2
    assert await sidebar(client, headers, web) == [("General", 0), ("Frontend", 1), ("QA", 2)]
    fetched = await client.get(url(web, f"/{qa['id']}"), headers=headers)
    assert fetched.json() == qa


async def test_space_names_are_unique_per_project(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    other = await make_project(db_session, organization=acme)
    await make_space(db_session, project=other, name="Frontend")
    await add(client, admin["headers"], web, "Frontend")  # fine: other project

    taken = await client.post(url(web), headers=admin["headers"], json={"name": "Frontend"})
    general = await client.post(url(web), headers=admin["headers"], json={"name": " General "})

    for response in (taken, general):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"
        assert response.json()["error"]["details"][0]["field"] == "name"
    assert len(await sidebar(client, admin["headers"], web)) == 2


@pytest.mark.parametrize(
    "body", [{}, {"name": " "}, {"name": "x" * 101}, {"name": "A", "position": 0}]
)
async def test_invalid_space_body(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project, body: dict
) -> None:
    assert (await client.post(url(web), headers=admin["headers"], json=body)).status_code == 422


async def test_list_with_task_counts_search_and_paging(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    db_session: AsyncSession,
    admin: dict[str, Any],
    web: Project,
) -> None:
    general = await db_session.scalar(select(Space).where(Space.project_id == web.id))
    frontend = await make_space(db_session, project=web, name="Frontend", position=2)
    await make_space(db_session, project=web, name="Backend", position=1)
    for _ in range(3):
        await make_task(db_session, project=web, space=frontend)
    await make_task(db_session, project=web, space=general)

    with count_queries(engine) as queries:
        response = await client.get(url(web), headers=admin["headers"])

    assert [(s["name"], s["task_count"]) for s in response.json()["items"]] == [
        ("General", 1),
        ("Backend", 0),
        ("Frontend", 3),
    ]
    assert response.json()["pagination"]["page_size"] == 50  # SPC-1's own default
    # user + project + permissions + COUNT + page (counts in the same SELECT)
    assert queries.count == 5, str(queries)

    search = await client.get(url(web, "?q=END"), headers=admin["headers"])
    assert [s["name"] for s in search.json()["items"]] == ["Backend", "Frontend"]
    page_2 = await client.get(url(web, "?page=2&page_size=2"), headers=admin["headers"])
    assert [s["name"] for s in page_2.json()["items"]] == ["Frontend"]
    assert (
        await client.get(url(web, "?page_size=101"), headers=admin["headers"])
    ).status_code == 422
    one = await client.get(url(web, f"/{frontend.id}"), headers=admin["headers"])
    assert one.json()["task_count"] == 3


async def test_spaces_of_other_projects_are_not_found(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    elsewhere = await make_space(
        db_session, project=await make_project(db_session, organization=acme)
    )
    stamp = {"name": "X", "expected_updated_at": "2026-01-01T00:00:00Z"}

    assert (
        await client.get(url(web, f"/{elsewhere.id}"), headers=admin["headers"])
    ).status_code == 404
    assert (
        await client.patch(url(web, f"/{elsewhere.id}"), headers=admin["headers"], json=stamp)
    ).status_code == 404


async def test_spaces_cannot_be_deleted(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], web: Project
) -> None:
    general = await db_session.scalar(select(Space).where(Space.project_id == web.id))

    response = await client.delete(url(web, f"/{general.id}"), headers=admin["headers"])

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


async def test_space_permissions(
    client: httpx.AsyncClient, db_session: AsyncSession, acme: Organization, web: Project
) -> None:
    general = await db_session.scalar(select(Space).where(Space.project_id == web.id))
    task_viewer = await member(db_session, acme, {"tasks": "view"}, web)
    space_viewer = await member(db_session, acme, {"spaces": "view"}, web)
    creator = await member(db_session, acme, {"spaces": "create"}, web)
    editor = await member(db_session, acme, {"spaces": "edit"}, web)
    teams_only = await member(db_session, acme, {"teams": "view"}, web)
    nobody = await outsider(db_session, acme)
    order = {"space_ids": [general.id]}

    assert (await client.get(url(web), headers=task_viewer)).status_code == 200  # the sidebar
    assert (await client.get(url(web, f"/{general.id}"), headers=task_viewer)).status_code == 200
    assert (await client.get(url(web), headers=space_viewer)).status_code == 200
    assert (await client.get(url(web), headers=teams_only)).status_code == 403
    assert (await client.get(url(web), headers=nobody)).status_code == 404
    assert (await client.post(url(web), headers=task_viewer, json={"name": "A"})).status_code == 403
    assert (await client.post(url(web), headers=creator, json={"name": "A"})).status_code == 201
    assert (await client.put(url(web, "/order"), headers=creator, json=order)).status_code == 403
    assert (await client.put(url(web, "/order"), headers=editor, json=order)).status_code == 422


# ── SPC-4 ─────────────────────────────────────────────────────────────────────


async def test_rename_space(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], web: Project
) -> None:
    space = await add(client, admin["headers"], web, "Frontend", description="UI")
    await make_task(db_session, project=web, space=await db_session.get(Space, space["id"]))
    path = url(web, f"/{space['id']}")

    renamed = await client.patch(
        path,
        headers=admin["headers"],
        json={"name": "Web UI", "expected_updated_at": space["updated_at"]},
    )
    stale = await client.patch(
        path,
        headers=admin["headers"],
        json={"description": "x", "expected_updated_at": space["updated_at"]},
    )
    taken = await client.patch(
        path,
        headers=admin["headers"],
        json={"name": "General", "expected_updated_at": renamed.json()["updated_at"]},
    )
    same_name = await client.patch(
        path,
        headers=admin["headers"],
        json={"name": "Web UI", "expected_updated_at": renamed.json()["updated_at"]},
    )

    assert renamed.status_code == 200
    body = renamed.json()
    assert (body["name"], body["description"], body["task_count"]) == ("Web UI", "UI", 1)
    assert body["updated_at"] > space["updated_at"]
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_update"
    assert stale.json()["error"]["details"]["current"]["name"] == "Web UI"
    assert taken.status_code == 409
    assert taken.json()["error"]["code"] == "conflict"
    assert same_name.status_code == 200  # its own name is not "taken"


# ── SPC-5 ─────────────────────────────────────────────────────────────────────


async def test_reorder_spaces(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], web: Project
) -> None:
    headers = admin["headers"]
    general_id = await db_session.scalar(select(Space.id).where(Space.project_id == web.id))
    frontend = await add(client, headers, web, "Frontend")
    qa = await add(client, headers, web, "QA")
    order = [qa["id"], general_id, frontend["id"]]

    first = await client.put(url(web, "/order"), headers=headers, json={"space_ids": order})
    again = await client.put(url(web, "/order"), headers=headers, json={"space_ids": order})

    assert first.status_code == 200
    assert [(s["name"], s["position"]) for s in first.json()["items"]] == [
        ("QA", 0),
        ("General", 1),
        ("Frontend", 2),
    ]
    assert first.json()["pagination"]["page_size"] == 50
    assert again.json() == first.json()  # a retry changes nothing
    # The order is not part of a rename's version: updated_at is untouched.
    qa_now = (await client.get(url(web, f"/{qa['id']}"), headers=headers)).json()
    assert qa_now["updated_at"] == qa["updated_at"]
    assert (
        await client.patch(
            url(web, f"/{qa['id']}"),
            headers=headers,
            json={"name": "Testing", "expected_updated_at": qa["updated_at"]},
        )
    ).status_code == 200
    # New spaces still go after the highest position.
    assert (await add(client, headers, web, "Docs"))["position"] == 3


async def test_reorder_needs_every_space_exactly_once(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    general_id = await db_session.scalar(select(Space.id).where(Space.project_id == web.id))
    frontend_id = (await add(client, admin["headers"], web, "Frontend"))["id"]
    foreign_id = (
        await make_space(db_session, project=await make_project(db_session, organization=acme))
    ).id

    cases = {
        "missing": [general_id],
        "not in this project": [general_id, frontend_id, foreign_id],
        "more than once": [general_id, frontend_id, frontend_id],
    }
    for expected, space_ids in cases.items():
        response = await client.put(
            url(web, "/order"), headers=admin["headers"], json={"space_ids": space_ids}
        )
        assert response.status_code == 422, space_ids
        assert response.json()["error"]["code"] == "invalid_reference"
        assert expected in response.json()["error"]["details"][0]["message"]

    for body in ({"space_ids": []}, {"space_ids": list(range(1, 502))}, {}):
        response = await client.put(url(web, "/order"), headers=admin["headers"], json=body)
        assert response.json()["error"]["code"] == "validation_error"
    assert await sidebar(client, admin["headers"], web) == [("General", 0), ("Frontend", 1)]


async def test_archived_project_spaces_are_read_only(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], web: Project
) -> None:
    space = await db_session.scalar(select(Space).where(Space.project_id == web.id))
    space_id, stamp = space.id, space.updated_at.isoformat()
    await db_session.execute(update(Project).where(Project.id == web.id).values(status="archived"))
    headers = admin["headers"]

    writes = [
        await client.post(url(web), headers=headers, json={"name": "A"}),
        await client.patch(
            url(web, f"/{space_id}"),
            headers=headers,
            json={"name": "X", "expected_updated_at": stamp},
        ),
        await client.put(url(web, "/order"), headers=headers, json={"space_ids": [space_id]}),
    ]

    assert [w.json()["error"]["code"] for w in writes] == ["project_archived"] * 3
    assert await sidebar(client, headers, web) == [("General", 0)]


# ── Concurrency: real, separate transactions ─────────────────────────────────

RACE_SLUG = "race-spaces"


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
            # Spaces, statuses and user_roles go with their project / user (CASCADE).
            await cleanup.execute(delete(Project).where(Project.organization_id.in_(org_ids)))
            await cleanup.execute(delete(User).where(User.organization_id.in_(org_ids)))
            await cleanup.execute(delete(Role).where(Role.organization_id.in_(org_ids)))
            await cleanup.execute(delete(Organization).where(Organization.slug == RACE_SLUG))
            await cleanup.commit()


@pytest.fixture
async def race(real_client: httpx.AsyncClient) -> dict[str, Any]:
    signed_up = await real_client.post(
        "/auth/register", json=REGISTER | {"organization_slug": RACE_SLUG}
    )
    headers = bearer(signed_up.json()["access_token"])
    project = await new_project(real_client, headers)
    return {"headers": headers, "spaces": f"/projects/{project['public_id']}/spaces"}


async def test_simultaneous_creates_get_different_positions(
    real_client: httpx.AsyncClient, engine: AsyncEngine, race: dict[str, Any]
) -> None:
    """Three creates start while the layout lock is held elsewhere, so all of
    them are in flight at once; each still gets its own position."""
    public_id = race["spaces"].split("/")[2]
    created: list[dict] = []

    async def create(name: str) -> None:
        response = await real_client.post(
            race["spaces"], headers=race["headers"], json={"name": name}
        )
        assert response.status_code == 201
        created.append(response.json())

    async with AsyncSession(engine) as holder:
        project_id = await holder.scalar(select(Project.id).where(Project.public_id == public_id))
        await space_repository.lock_layout(holder, project_id)
        async with anyio.create_task_group() as group:
            for n in range(3):
                group.start_soon(create, f"Space {n}")
            await anyio.sleep(0.5)
            assert created == []  # all three are waiting
            await holder.commit()

    assert sorted(space["position"] for space in created) == [1, 2, 3]


async def test_simultaneous_same_name_one_wins(
    real_client: httpx.AsyncClient, race: dict[str, Any]
) -> None:
    statuses: list[int] = []

    async def create() -> None:
        response = await real_client.post(
            race["spaces"], headers=race["headers"], json={"name": "Frontend"}
        )
        statuses.append(response.status_code)

    async with anyio.create_task_group() as group:
        group.start_soon(create)
        group.start_soon(create)

    assert sorted(statuses) == [201, 409]


async def test_create_waits_for_a_reorder_in_progress(
    real_client: httpx.AsyncClient, engine: AsyncEngine, race: dict[str, Any]
) -> None:
    """A create can't slip in between a reorder's check and its update: it
    waits for the reorder's layout lock, then goes after the new order."""
    public_id = race["spaces"].split("/")[2]

    async with AsyncSession(engine) as reorder:
        project_id = await reorder.scalar(select(Project.id).where(Project.public_id == public_id))
        await reorder.execute(
            select(Space.id).where(Space.project_id == project_id).with_for_update()
        )
        await space_repository.lock_layout(reorder, project_id)
        await reorder.execute(
            update(Space).where(Space.project_id == project_id).values(position=7)
        )
        async with anyio.create_task_group() as group:
            responses: list[httpx.Response] = []

            async def create() -> None:
                responses.append(
                    await real_client.post(
                        race["spaces"], headers=race["headers"], json={"name": "New"}
                    )
                )

            group.start_soon(create)
            await anyio.sleep(0.5)
            assert responses == []  # waiting on the layout lock
            await reorder.commit()

    assert responses[0].status_code == 201
    assert responses[0].json()["position"] == 8  # after the committed order
