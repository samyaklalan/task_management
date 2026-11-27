"""M11: STS-1 to STS-5 (task statuses, the board columns)."""

from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.session import get_session
from app.main import create_app
from app.models import Organization, Project, Role, Task, TaskStatus, User
from app.repositories import task_status as status_repository
from tests.api.project_helpers import REGISTER, bearer, member, new_project, outsider
from tests.factories import make_project, make_task, make_task_status
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
    """A project made through the API: To Do, In Progress, Done at 0, 1, 2."""
    created = await new_project(client, admin["headers"])
    return await db_session.scalar(select(Project).where(Project.public_id == created["public_id"]))


def url(project: Project, tail: str = "") -> str:
    return f"/projects/{project.public_id}/statuses{tail}"


async def board(client: httpx.AsyncClient, headers: dict, project: Project) -> list[dict]:
    response = await client.get(url(project), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def ids(session: AsyncSession, project: Project) -> dict[str, int]:
    """{status name: id} for the project."""
    rows = await session.execute(
        select(TaskStatus.name, TaskStatus.id).where(TaskStatus.project_id == project.id)
    )
    return dict(rows.all())


async def tasks_in(session: AsyncSession, status_id: int) -> int:
    return await session.scalar(
        select(func.count()).select_from(Task).where(Task.status_id == status_id)
    )


# ── STS-1 / STS-2 ─────────────────────────────────────────────────────────────


async def test_list_board_with_task_counts(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    statuses = {
        s.name: s
        for s in await db_session.scalars(select(TaskStatus).where(TaskStatus.project_id == web.id))
    }
    for _ in range(2):
        await make_task(db_session, project=web, status=statuses["To Do"])
    await make_task(db_session, project=web, status=statuses["Done"])
    await make_task_status(db_session, project=await make_project(db_session, organization=acme))

    with count_queries(engine) as queries:
        response = await client.get(url(web), headers=admin["headers"])

    body = response.json()
    assert set(body) == {"items"}  # not paginated
    assert [(s["name"], s["category"], s["position"], s["task_count"]) for s in body["items"]] == [
        ("To Do", "todo", 0, 2),
        ("In Progress", "in_progress", 1, 0),
        ("Done", "done", 2, 1),
    ]
    assert set(body["items"][0]) == {
        "id",
        "name",
        "color",
        "category",
        "position",
        "task_count",
        "updated_at",
    }
    # user + project + permissions + one SELECT with the counts
    assert queries.count == 4, str(queries)


async def test_create_status_at_the_end(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project
) -> None:
    response = await client.post(
        url(web),
        headers=admin["headers"],
        json={"name": " In Review ", "color": "#a855f7", "category": "in_progress"},
    )

    assert response.status_code == 201
    body = response.json()
    assert (
        body["name"],
        body["color"],
        body["category"],
        body["position"],
        body["task_count"],
    ) == (
        "In Review",
        "#A855F7",  # stored uppercase
        "in_progress",
        3,
        0,
    )
    # Names don't have to be unique.
    again = await client.post(
        url(web),
        headers=admin["headers"],
        json={"name": "In Review", "color": "#A855F7", "category": "in_progress"},
    )
    assert again.status_code == 201
    assert again.json()["position"] == 4


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"name": " "}, "name"),
        ({"name": "x" * 51}, "name"),
        ({"color": "blue"}, "color"),
        ({"color": "#12345"}, "color"),
        ({"color": "#GGGGGG"}, "color"),
        ({"category": "blocked"}, "category"),
        ({"position": 0}, "position"),
    ],
)
async def test_invalid_status_body(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project, changes: dict, field: str
) -> None:
    body = {"name": "Review", "color": "#A855F7", "category": "in_progress"} | changes

    response = await client.post(url(web), headers=admin["headers"], json=body)

    assert response.status_code == 422
    assert [d["field"] for d in response.json()["error"]["details"]] == [field]


async def test_status_permissions(
    client: httpx.AsyncClient, db_session: AsyncSession, acme: Organization, web: Project
) -> None:
    task_viewer = await member(db_session, acme, {"tasks": "view"}, web)
    status_viewer = await member(db_session, acme, {"task_statuses": "view"}, web)
    creator = await member(db_session, acme, {"task_statuses": "create"}, web)
    deleter = await member(db_session, acme, {"task_statuses": "delete"}, web)
    teams_only = await member(db_session, acme, {"teams": "view"}, web)
    nobody = await outsider(db_session, acme)
    body = {"name": "Review", "color": "#A855F7", "category": "in_progress"}
    doing = (await ids(db_session, web))["In Progress"]

    assert (await client.get(url(web), headers=task_viewer)).status_code == 200  # the board
    assert (await client.get(url(web), headers=status_viewer)).status_code == 200
    assert (await client.get(url(web), headers=teams_only)).status_code == 403
    assert (await client.get(url(web), headers=nobody)).status_code == 404
    assert (await client.post(url(web), headers=task_viewer, json=body)).status_code == 403
    assert (await client.post(url(web), headers=creator, json=body)).status_code == 201
    assert (await client.delete(url(web, f"/{doing}"), headers=creator)).status_code == 403
    assert (await client.delete(url(web, f"/{doing}"), headers=deleter)).status_code == 204


# ── STS-3 ─────────────────────────────────────────────────────────────────────


async def test_update_status(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], web: Project
) -> None:
    doing = next(
        s for s in await board(client, admin["headers"], web) if s["name"] == "In Progress"
    )
    await make_task(db_session, project=web, status=await db_session.get(TaskStatus, doing["id"]))
    path = url(web, f"/{doing['id']}")

    renamed = await client.patch(
        path,
        headers=admin["headers"],
        json={
            "name": "Code Review",
            "color": "#9333ea",
            "category": "done",
            "expected_updated_at": doing["updated_at"],
        },
    )
    stale = await client.patch(
        path,
        headers=admin["headers"],
        json={"name": "Old", "expected_updated_at": doing["updated_at"]},
    )

    assert renamed.status_code == 200
    body = renamed.json()
    assert (body["name"], body["color"], body["category"], body["task_count"]) == (
        "Code Review",
        "#9333EA",
        "done",
        1,
    )
    assert body["position"] == 1 and body["updated_at"] > doing["updated_at"]
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_update"
    assert stale.json()["error"]["details"]["current"]["name"] == "Code Review"
    for null_field in ("name", "color", "category"):
        response = await client.patch(
            path,
            headers=admin["headers"],
            json={null_field: None, "expected_updated_at": body["updated_at"]},
        )
        assert response.status_code == 422, null_field


async def test_statuses_of_other_projects_are_not_found(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    elsewhere = await make_task_status(
        db_session, project=await make_project(db_session, organization=acme)
    )
    stamp = {"name": "X", "expected_updated_at": "2026-01-01T00:00:00Z"}

    assert (
        await client.patch(url(web, f"/{elsewhere.id}"), headers=admin["headers"], json=stamp)
    ).status_code == 404
    assert (
        await client.delete(url(web, f"/{elsewhere.id}"), headers=admin["headers"])
    ).status_code == 404
    assert (
        await client.delete(url(web, "/999999999"), headers=admin["headers"])
    ).status_code == 404


# ── STS-4 ─────────────────────────────────────────────────────────────────────


async def test_reorder_statuses(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], web: Project
) -> None:
    headers = admin["headers"]
    before = {s["name"]: s for s in await board(client, headers, web)}
    order = [before["Done"]["id"], before["To Do"]["id"], before["In Progress"]["id"]]

    first = await client.put(url(web, "/order"), headers=headers, json={"status_ids": order})
    again = await client.put(url(web, "/order"), headers=headers, json={"status_ids": order})

    assert first.status_code == 200
    assert [(s["name"], s["position"]) for s in first.json()["items"]] == [
        ("Done", 0),
        ("To Do", 1),
        ("In Progress", 2),
    ]
    assert again.json() == first.json()  # a retry changes nothing
    done_now = next(s for s in first.json()["items"] if s["name"] == "Done")
    assert done_now["updated_at"] == before["Done"]["updated_at"]  # order isn't a rename

    foreign = (await make_task_status(db_session, project=await make_project(db_session))).id
    cases = {
        "missing": order[:2],
        "not in this project": [*order, foreign],
        "more than once": [*order, order[0]],
    }
    for expected, status_ids in cases.items():
        response = await client.put(
            url(web, "/order"), headers=headers, json={"status_ids": status_ids}
        )
        assert response.status_code == 422, status_ids
        assert response.json()["error"]["code"] == "invalid_reference"
        assert expected in response.json()["error"]["details"][0]["message"]
    empty = await client.put(url(web, "/order"), headers=headers, json={"status_ids": []})
    assert empty.json()["error"]["code"] == "validation_error"


# ── STS-5 ─────────────────────────────────────────────────────────────────────


async def test_delete_unused_status(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], web: Project
) -> None:
    doing = (await ids(db_session, web))["In Progress"]

    response = await client.delete(url(web, f"/{doing}"), headers=admin["headers"])

    assert response.status_code == 204
    assert [s["name"] for s in await board(client, admin["headers"], web)] == ["To Do", "Done"]
    assert (await client.delete(url(web, f"/{doing}"), headers=admin["headers"])).status_code == 404


async def test_status_in_use_needs_move_to(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], web: Project
) -> None:
    status_ids = await ids(db_session, web)
    todo, done = status_ids["To Do"], status_ids["Done"]
    tasks = [
        await make_task(db_session, project=web, status=await db_session.get(TaskStatus, todo))
        for _ in range(3)
    ]
    before = {task.id: task.updated_at for task in tasks}

    refused = await client.delete(url(web, f"/{todo}"), headers=admin["headers"])

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "status_in_use"
    assert refused.json()["error"]["details"] == {"task_count": 3}

    moved = await client.delete(url(web, f"/{todo}?move_to={done}"), headers=admin["headers"])

    assert moved.status_code == 204
    assert await tasks_in(db_session, done) == 3
    for task_id, updated_at in before.items():
        task = await db_session.get(Task, task_id, populate_existing=True)
        assert task.status_id == done
        assert task.updated_at > updated_at  # open edit forms get a fair stale_update
    assert [s["name"] for s in await board(client, admin["headers"], web)] == [
        "In Progress",
        "Done",
    ]


async def test_move_to_must_be_another_status_of_this_project(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    todo = (await ids(db_session, web))["To Do"]
    await make_task(db_session, project=web, status=await db_session.get(TaskStatus, todo))
    foreign = (
        await make_task_status(
            db_session, project=await make_project(db_session, organization=acme)
        )
    ).id

    for move_to in (todo, foreign, 999_999_999):
        response = await client.delete(
            url(web, f"/{todo}?move_to={move_to}"), headers=admin["headers"]
        )
        assert response.status_code == 422, move_to
        assert response.json()["error"]["code"] == "invalid_reference"
        assert response.json()["error"]["details"][0]["field"] == "move_to"
    for bad in ("0", "abc"):
        response = await client.delete(url(web, f"/{todo}?move_to={bad}"), headers=admin["headers"])
        assert response.json()["error"]["code"] == "validation_error"
    assert await tasks_in(db_session, todo) == 1  # nothing moved


async def test_the_last_status_stays(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], web: Project
) -> None:
    status_ids = await ids(db_session, web)
    for name in ("To Do", "In Progress"):
        assert (
            await client.delete(url(web, f"/{status_ids[name]}"), headers=admin["headers"])
        ).status_code == 204

    last = await client.delete(url(web, f"/{status_ids['Done']}"), headers=admin["headers"])

    assert last.status_code == 409
    assert last.json()["error"]["code"] == "last_status"


async def test_archived_project_statuses_are_read_only(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], web: Project
) -> None:
    status = await db_session.scalar(select(TaskStatus).where(TaskStatus.project_id == web.id))
    status_id, stamp = status.id, status.updated_at.isoformat()
    all_ids = list((await ids(db_session, web)).values())
    await db_session.execute(update(Project).where(Project.id == web.id).values(status="archived"))
    headers = admin["headers"]

    writes = [
        await client.post(
            url(web), headers=headers, json={"name": "A", "color": "#000000", "category": "todo"}
        ),
        await client.patch(
            url(web, f"/{status_id}"),
            headers=headers,
            json={"name": "X", "expected_updated_at": stamp},
        ),
        await client.put(url(web, "/order"), headers=headers, json={"status_ids": all_ids}),
        await client.delete(url(web, f"/{status_id}"), headers=headers),
    ]

    assert [w.json()["error"]["code"] for w in writes] == ["project_archived"] * 4
    assert len(await board(client, headers, web)) == 3


# ── Concurrency: real, separate transactions ─────────────────────────────────

RACE_SLUG = "race-statuses"


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
            project_ids = select(Project.id).where(Project.organization_id.in_(org_ids))
            # Tasks first: their status foreign key doesn't cascade.
            await cleanup.execute(delete(Task).where(Task.project_id.in_(project_ids)))
            await cleanup.execute(delete(Project).where(Project.organization_id.in_(org_ids)))
            await cleanup.execute(delete(User).where(User.organization_id.in_(org_ids)))
            await cleanup.execute(delete(Role).where(Role.organization_id.in_(org_ids)))
            await cleanup.execute(delete(Organization).where(Organization.slug == RACE_SLUG))
            await cleanup.commit()


@pytest.fixture
async def race(real_client: httpx.AsyncClient, engine: AsyncEngine) -> dict[str, Any]:
    signed_up = await real_client.post(
        "/auth/register", json=REGISTER | {"organization_slug": RACE_SLUG}
    )
    headers = bearer(signed_up.json()["access_token"])
    project = await new_project(real_client, headers)
    path = f"/projects/{project['public_id']}/statuses"
    statuses = (await real_client.get(path, headers=headers)).json()["items"]
    async with AsyncSession(engine) as setup:
        project_id = await setup.scalar(
            select(Project.id).where(Project.public_id == project["public_id"])
        )
    return {
        "headers": headers,
        "path": path,
        "project_id": project_id,
        "ids": {s["name"]: s["id"] for s in statuses},
    }


async def test_two_deletes_never_remove_the_last_status(
    real_client: httpx.AsyncClient, engine: AsyncEngine, race: dict[str, Any]
) -> None:
    """Delete one status first, then delete the remaining two at the same
    time. Exactly one succeeds; the project keeps a status."""
    headers, path, ids_ = race["headers"], race["path"], race["ids"]
    assert (
        await real_client.delete(f"{path}/{ids_['In Progress']}", headers=headers)
    ).status_code == 204
    statuses: list[int] = []

    async def remove(status_id: int) -> None:
        statuses.append(
            (await real_client.delete(f"{path}/{status_id}", headers=headers)).status_code
        )

    async with AsyncSession(engine) as holder:
        # Hold the board lock so both deletes are in flight before either runs.
        await status_repository.lock_board(holder, race["project_id"])
        async with anyio.create_task_group() as group:
            group.start_soon(remove, ids_["To Do"])
            group.start_soon(remove, ids_["Done"])
            await anyio.sleep(0.5)
            assert statuses == []
            await holder.commit()

    assert sorted(statuses) == [204, 409]
    remaining = (await real_client.get(path, headers=headers)).json()["items"]
    assert len(remaining) == 1


async def test_simultaneous_creates_get_different_positions(
    real_client: httpx.AsyncClient, engine: AsyncEngine, race: dict[str, Any]
) -> None:
    created: list[dict] = []

    async def create(name: str) -> None:
        response = await real_client.post(
            race["path"],
            headers=race["headers"],
            json={"name": name, "color": "#A855F7", "category": "in_progress"},
        )
        assert response.status_code == 201
        created.append(response.json())

    async with AsyncSession(engine) as holder:
        await status_repository.lock_board(holder, race["project_id"])
        async with anyio.create_task_group() as group:
            for n in range(3):
                group.start_soon(create, f"Review {n}")
            await anyio.sleep(0.5)
            assert created == []
            await holder.commit()

    assert sorted(s["position"] for s in created) == [3, 4, 5]


async def test_delete_waits_for_a_task_moving_into_the_status(
    real_client: httpx.AsyncClient, engine: AsyncEngine, race: dict[str, Any]
) -> None:
    """A task is being moved into "To Do" (not yet committed) when someone
    deletes "To Do". The delete's FOR UPDATE on the status waits for the move,
    then counts the task: 409 status_in_use, not a 500 from the foreign key."""
    todo, done = race["ids"]["To Do"], race["ids"]["Done"]
    async with AsyncSession(engine, expire_on_commit=False) as setup:
        project = await setup.get(Project, race["project_id"])
        task = await make_task(setup, project=project, status=await setup.get(TaskStatus, done))
        await setup.commit()
    responses: list[httpx.Response] = []

    async def delete_todo() -> None:
        responses.append(
            await real_client.delete(f"{race['path']}/{todo}", headers=race["headers"])
        )

    async with AsyncSession(engine) as mover:
        await mover.execute(update(Task).where(Task.id == task.id).values(status_id=todo))
        async with anyio.create_task_group() as group:
            group.start_soon(delete_todo)
            await anyio.sleep(0.5)
            assert responses == []  # waiting for the move's lock on the status row
            await mover.commit()

    assert responses[0].status_code == 409
    assert responses[0].json()["error"]["code"] == "status_in_use"
    assert responses[0].json()["error"]["details"] == {"task_count": 1}
