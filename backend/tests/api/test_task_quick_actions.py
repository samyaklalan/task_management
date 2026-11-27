"""M12b: the one-field quick actions TSK-5 (status), TSK-6 (assignee),
TSK-7 (priority) and TSK-9 (space)."""

from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.session import get_session
from app.main import create_app
from app.models import Organization, Project, Role, Task, User
from tests.api.project_helpers import (
    REGISTER,
    bearer,
    member,
    new_project,
    new_task,
    outsider,
    project_user,
    status_ids,
    tasks_url,
)
from tests.factories import make_project, make_space, make_task_status, make_user
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


@pytest.fixture
async def task(client: httpx.AsyncClient, admin: dict[str, Any], web: Project) -> dict:
    """WEB-1, in "To Do" / "General", medium, unassigned."""
    return await new_task(client, admin["headers"], web, title="Fix login redirect")


async def act(
    client: httpx.AsyncClient, headers: dict, project: Project, action: str, **body: Any
) -> httpx.Response:
    return await client.patch(tasks_url(project, f"/1/{action}"), headers=headers, json=body)


# ── Each action ───────────────────────────────────────────────────────────────


async def test_change_status(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    db_session: AsyncSession,
    admin: dict[str, Any],
    web: Project,
    task: dict,
) -> None:
    done = (await status_ids(db_session, web))["Done"]

    with count_queries(engine) as queries:
        response = await act(client, admin["headers"], web, "status", status_id=done)

    assert response.status_code == 200
    body = response.json()
    assert body["status"]["name"] == "Done"
    assert body["updated_at"] > task["updated_at"]
    # The answer is the full task page (TSK-3).
    assert set(body) == set(task)
    assert (body["title"], body["space"], body["priority"]) == (
        task["title"],
        task["space"],
        task["priority"],
    )
    # user + project + permissions + project lock + task + status check
    # + UPDATE + task detail
    assert queries.count == 8, str(queries)


async def test_change_assignee_and_unassign(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
    task: dict,
) -> None:
    ravi = await project_user(db_session, acme, web, "Ravi Kumar")

    assigned = await act(client, admin["headers"], web, "assignee", assignee_id=ravi)
    unassigned = await act(client, admin["headers"], web, "assignee", assignee_id=None)

    assert assigned.status_code == 200
    assert assigned.json()["assignee"] == {"id": ravi, "name": "Ravi Kumar"}
    assert unassigned.status_code == 200
    assert unassigned.json()["assignee"] is None


async def test_change_priority(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project, task: dict
) -> None:
    response = await act(client, admin["headers"], web, "priority", priority="urgent")

    assert response.status_code == 200
    assert response.json()["priority"] == "urgent"


async def test_change_space_keeps_the_number(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    web: Project,
    task: dict,
) -> None:
    frontend = await make_space(db_session, project=web, name="Frontend", position=1)

    response = await act(client, admin["headers"], web, "space", space_id=frontend.id)

    assert response.status_code == 200
    assert response.json()["space"] == {"id": frontend.id, "name": "Frontend"}
    assert (response.json()["number"], response.json()["key"]) == (1, "WEB-1")


# ── Validation ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("action", "body"),
    [
        ("status", {}),
        ("status", {"status_id": None}),
        ("status", {"status_id": 0}),
        ("assignee", {}),  # assignee_id is required; null means "unassign"
        ("priority", {"priority": "critical"}),
        ("priority", {"priority": None}),
        ("space", {"space_id": "abc"}),
        ("status", {"status_id": 1, "title": "sneaky"}),  # one field only
        ("priority", {"priority": "low", "expected_updated_at": "2026-11-16T10:00:00"}),
    ],
)
async def test_invalid_bodies(
    client: httpx.AsyncClient,
    admin: dict[str, Any],
    web: Project,
    task: dict,
    action: str,
    body: dict,
) -> None:
    response = await act(client, admin["headers"], web, action, **body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_references_must_belong_to_the_project(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
    task: dict,
) -> None:
    other = await make_project(db_session, organization=acme)
    foreign_status = (await make_task_status(db_session, project=other)).id
    foreign_space = (await make_space(db_session, project=other)).id
    no_access = (await make_user(db_session, organization=acme)).id
    inactive = await project_user(db_session, acme, web, "Gone", is_active=False)

    cases = [
        ("status", {"status_id": foreign_status}, "status_id"),
        ("space", {"space_id": foreign_space}, "space_id"),
        ("assignee", {"assignee_id": no_access}, "assignee_id"),
        ("assignee", {"assignee_id": inactive}, "assignee_id"),
    ]
    for action, body, field in cases:
        response = await act(client, admin["headers"], web, action, **body)
        assert response.status_code == 422, body
        assert response.json()["error"]["code"] == "invalid_reference"
        assert response.json()["error"]["details"][0]["field"] == field

    unchanged = (await client.get(tasks_url(web, "/1"), headers=admin["headers"])).json()
    assert unchanged == task


async def test_unknown_task_is_not_found(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project
) -> None:
    for action, body in (
        ("status", {"status_id": 1}),
        ("assignee", {"assignee_id": None}),
        ("priority", {"priority": "low"}),
        ("space", {"space_id": 1}),
    ):
        response = await act(client, admin["headers"], web, action, **body)
        assert response.status_code == 404, action


# ── Concurrency rules (api.md section 6) ──────────────────────────────────────


async def test_expected_updated_at_is_checked_when_sent(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project, task: dict
) -> None:
    first = await act(
        client, admin["headers"], web, "priority", priority="high",
        expected_updated_at=task["updated_at"],
    )  # fmt: skip
    stale = await act(
        client, admin["headers"], web, "priority", priority="low",
        expected_updated_at=task["updated_at"],
    )  # fmt: skip

    assert first.status_code == 200
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_update"
    assert stale.json()["error"]["details"]["current"]["priority"] == "high"


async def test_without_expected_updated_at_the_last_write_wins(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project, task: dict
) -> None:
    await act(client, admin["headers"], web, "priority", priority="high")

    response = await act(client, admin["headers"], web, "priority", priority="low")

    assert response.status_code == 200
    assert response.json()["priority"] == "low"


async def test_same_value_changes_nothing(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    db_session: AsyncSession,
    admin: dict[str, Any],
    web: Project,
    task: dict,
) -> None:
    """Dropping a card back on its own column writes nothing, so nobody's open
    edit form becomes stale."""
    todo = (await status_ids(db_session, web))["To Do"]

    with count_queries(engine) as queries:
        response = await act(client, admin["headers"], web, "status", status_id=todo)

    assert response.status_code == 200
    assert response.json()["updated_at"] == task["updated_at"]
    assert not any(q.lstrip().upper().startswith("UPDATE TASKS") for q in queries.statements)
    edit = await client.patch(
        tasks_url(web, "/1"),
        headers=admin["headers"],
        json={"title": "Still my version", "expected_updated_at": task["updated_at"]},
    )
    assert edit.status_code == 200
    for action, body in (
        ("priority", {"priority": "medium"}),
        ("assignee", {"assignee_id": None}),
        ("space", {"space_id": task["space"]["id"]}),
    ):
        same = await act(client, admin["headers"], web, action, **body)
        assert same.json()["updated_at"] == edit.json()["updated_at"], action


async def test_quick_action_makes_an_open_edit_form_stale(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    web: Project,
    task: dict,
) -> None:
    """Someone drags the card while another person has the edit form open: the
    drag wins (no version sent), and the form's save is a fair stale_update."""
    done = (await status_ids(db_session, web))["Done"]
    await act(client, admin["headers"], web, "status", status_id=done)

    save = await client.patch(
        tasks_url(web, "/1"),
        headers=admin["headers"],
        json={"title": "Edited in the form", "expected_updated_at": task["updated_at"]},
    )

    assert save.status_code == 409
    assert save.json()["error"]["details"]["current"]["status"]["name"] == "Done"


# ── Permissions and archived projects ─────────────────────────────────────────


async def test_quick_actions_need_tasks_edit(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    acme: Organization,
    web: Project,
    task: dict,
) -> None:
    viewer = await member(db_session, acme, {"tasks": "view,create,delete"}, web)
    editor = await member(db_session, acme, {"tasks": "edit"}, web)
    nobody = await outsider(db_session, acme)

    assert (await act(client, nobody, web, "priority", priority="low")).status_code == 404
    assert (await act(client, viewer, web, "priority", priority="low")).status_code == 403
    assert (await act(client, editor, web, "priority", priority="low")).status_code == 200


async def test_archived_project_refuses_quick_actions(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    web: Project,
    task: dict,
) -> None:
    done = (await status_ids(db_session, web))["Done"]
    await db_session.execute(update(Project).where(Project.id == web.id).values(status="archived"))

    responses = [
        await act(client, admin["headers"], web, "status", status_id=done),
        await act(client, admin["headers"], web, "assignee", assignee_id=None),
        await act(client, admin["headers"], web, "priority", priority="low"),
        await act(client, admin["headers"], web, "space", space_id=task["space"]["id"]),
    ]

    assert [r.json()["error"]["code"] for r in responses] == ["project_archived"] * 4


# ── Real, separate transactions ───────────────────────────────────────────────

RACE_SLUG = "race-quick-actions"


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
    path = f"/projects/{project['public_id']}/tasks"
    task = (await real_client.post(path, headers=headers, json={"title": "Race"})).json()
    statuses = (
        await real_client.get(f"/projects/{project['public_id']}/statuses", headers=headers)
    ).json()["items"]
    return {
        "headers": headers,
        "task": task,
        "path": f"{path}/1",
        "ids": {s["name"]: s["id"] for s in statuses},
    }


async def test_two_drags_without_a_version_both_succeed(
    real_client: httpx.AsyncClient, race: dict[str, Any]
) -> None:
    """Last write wins: both 200, and the card ends in one of the two columns."""
    results: dict[str, int] = {}

    async def drag(name: str) -> None:
        response = await real_client.patch(
            f"{race['path']}/status",
            headers=race["headers"],
            json={"status_id": race["ids"][name]},
        )
        results[name] = response.status_code

    async with anyio.create_task_group() as group:
        group.start_soon(drag, "In Progress")
        group.start_soon(drag, "Done")

    assert results == {"In Progress": 200, "Done": 200}
    final = (await real_client.get(race["path"], headers=race["headers"])).json()
    assert final["status"]["name"] in {"In Progress", "Done"}


async def test_two_drags_with_the_same_version_one_wins(
    real_client: httpx.AsyncClient, engine: AsyncEngine, race: dict[str, Any]
) -> None:
    """Both drags pass the early version check while the task row is locked
    elsewhere, so both reach the UPDATE. The version inside the UPDATE's WHERE
    then lets exactly one through."""
    results: dict[str, int] = {}

    async def drag(name: str) -> None:
        response = await real_client.patch(
            f"{race['path']}/status",
            headers=race["headers"],
            json={
                "status_id": race["ids"][name],
                "expected_updated_at": race["task"]["updated_at"],
            },
        )
        results[name] = response.status_code

    async with AsyncSession(engine) as holder:
        await holder.execute(
            select(Task.id).where(Task.number == 1, Task.title == "Race").with_for_update()
        )
        async with anyio.create_task_group() as group:
            group.start_soon(drag, "In Progress")
            group.start_soon(drag, "Done")
            await anyio.sleep(0.5)
            assert results == {}  # both are waiting at their UPDATE
            await holder.commit()

    assert sorted(results.values()) == [200, 409]
    winner = next(name for name, code in results.items() if code == 200)
    final = (await real_client.get(race["path"], headers=race["headers"])).json()
    assert final["status"]["name"] == winner
