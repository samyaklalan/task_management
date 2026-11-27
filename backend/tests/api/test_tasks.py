"""M12a: TSK-1 list, TSK-2 create, TSK-3 details, TSK-4 edit form, TSK-8 delete."""

from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.session import get_session
from app.main import create_app
from app.models import (
    Organization,
    Project,
    Role,
    Space,
    Task,
    TaskComment,
    TaskStatus,
    User,
    UserRole,
)
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
from tests.factories import (
    make_organization,
    make_project,
    make_space,
    make_task_comment,
    make_task_status,
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
    """Project WEB, made through the API: "General" space, To Do / In Progress / Done."""
    created = await new_project(client, admin["headers"])
    return await db_session.scalar(select(Project).where(Project.public_id == created["public_id"]))


async def general_space(session: AsyncSession, project: Project) -> int:
    return await session.scalar(
        select(Space.id).where(Space.project_id == project.id, Space.name == "General")
    )


async def listed(client: httpx.AsyncClient, headers: dict, project: Project, query: str = ""):
    response = await client.get(tasks_url(project, query), headers=headers)
    assert response.status_code == 200, response.text
    return [task["key"] for task in response.json()["items"]]


# ── TSK-2 create ──────────────────────────────────────────────────────────────


async def test_create_with_defaults(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], web: Project
) -> None:
    project_before = (
        await client.get(f"/projects/{web.public_id}", headers=admin["headers"])
    ).json()

    first = await new_task(client, admin["headers"], web, title="  Fix login redirect ")
    second = await new_task(client, admin["headers"], web)

    assert (first["number"], first["key"], first["title"]) == (1, "WEB-1", "Fix login redirect")
    assert (second["number"], second["key"]) == (2, "WEB-2")
    assert first["priority"] == "medium"
    assert first["status"] == {
        "id": (await status_ids(db_session, web))["To Do"],
        "name": "To Do",
        "color": "#64748B",
        "category": "todo",
    }
    assert first["space"] == {"id": await general_space(db_session, web), "name": "General"}
    assert first["assignee"] is None and first["description"] is None
    assert first["created_by"]["name"] == "Asha Rao"
    assert first["comment_count"] == 0
    assert set(first) == {
        "number", "key", "title", "description", "priority", "space", "status", "assignee",
        "created_by", "start_date", "end_date", "comment_count", "created_at", "updated_at",
    }  # fmt: skip
    # Taking a number isn't an edit of the project: its version stays the same.
    project_after = (
        await client.get(f"/projects/{web.public_id}", headers=admin["headers"])
    ).json()
    assert project_after["updated_at"] == project_before["updated_at"]
    assert project_after["task_counts"]["todo"] == 2


async def test_create_with_every_field(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    frontend = await make_space(db_session, project=web, name="Frontend", position=1)
    doing = (await status_ids(db_session, web))["In Progress"]
    ravi = await project_user(db_session, acme, web, "Ravi Kumar")

    body = await new_task(
        client,
        admin["headers"],
        web,
        title="Fix login redirect",
        description="After login, users land on /undefined.",
        space_id=frontend.id,
        status_id=doing,
        assignee_id=ravi,
        priority="high",
        start_date="2026-12-01",
        end_date="2026-12-05",
    )

    assert body["space"] == {"id": frontend.id, "name": "Frontend"}
    assert body["status"]["name"] == "In Progress"
    assert body["assignee"] == {"id": ravi, "name": "Ravi Kumar"}
    assert (body["priority"], body["start_date"], body["end_date"]) == (
        "high",
        "2026-12-01",
        "2026-12-05",
    )
    assert body["description"] == "After login, users land on /undefined."


async def test_default_status_and_space_follow_the_spec(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], web: Project
) -> None:
    ids = await status_ids(db_session, web)
    # No todo-category status left: the first status by position is used.
    await db_session.execute(
        update(TaskStatus).where(TaskStatus.id == ids["To Do"]).values(category="done")
    )
    await db_session.execute(
        update(TaskStatus).where(TaskStatus.id == ids["In Progress"]).values(position=-0)
    )
    # A space before "General" becomes the default.
    await db_session.execute(update(Space).where(Space.project_id == web.id).values(position=5))
    first_space = await make_space(db_session, project=web, name="Inbox", position=0)

    body = await new_task(client, admin["headers"], web)

    assert body["status"]["name"] in {"To Do", "In Progress"}
    assert body["status"]["id"] == await db_session.scalar(
        select(TaskStatus.id)
        .where(TaskStatus.project_id == web.id)
        .order_by(TaskStatus.position, TaskStatus.id)
        .limit(1)
    )
    assert body["space"] == {"id": first_space.id, "name": "Inbox"}

    # A todo status further right still wins over earlier non-todo ones.
    backlog = await make_task_status(
        db_session, project=web, name="Backlog", category="todo", position=9
    )
    assert (await new_task(client, admin["headers"], web))["status"]["id"] == backlog.id


async def test_create_with_invalid_references(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    other_project = await make_project(db_session, organization=acme)
    foreign_status = (await make_task_status(db_session, project=other_project)).id
    foreign_space = (await make_space(db_session, project=other_project)).id
    no_role = (await make_user(db_session, organization=acme)).id
    elsewhere = await project_user(db_session, acme, other_project, "Elsewhere")
    inactive = await project_user(db_session, acme, web, "Gone", is_active=False)
    foreign_user = (
        await make_user(db_session, organization=await make_organization(db_session))
    ).id

    cases = [
        ({"status_id": foreign_status}, "status_id"),
        ({"status_id": 999_999_999}, "status_id"),
        ({"space_id": foreign_space}, "space_id"),
        ({"assignee_id": no_role}, "assignee_id"),
        ({"assignee_id": elsewhere}, "assignee_id"),
        ({"assignee_id": inactive}, "assignee_id"),
        ({"assignee_id": foreign_user}, "assignee_id"),
    ]
    for body, field in cases:
        response = await client.post(
            tasks_url(web), headers=admin["headers"], json={"title": "X"} | body
        )
        assert response.status_code == 422, body
        assert response.json()["error"]["code"] == "invalid_reference"
        assert response.json()["error"]["details"][0]["field"] == field

    # Refused creates took no number.
    assert (await new_task(client, admin["headers"], web))["number"] == 1


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"title": "  "}, "title"),
        ({"title": "x" * 256}, "title"),
        ({"priority": "critical"}, "priority"),
        ({"description": "x" * 10_001}, "description"),
        ({"number": 7}, "number"),
        ({"status_id": 0}, "status_id"),
    ],
)
async def test_invalid_create_body(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project, changes: dict, field: str
) -> None:
    response = await client.post(
        tasks_url(web), headers=admin["headers"], json={"title": "A"} | changes
    )

    assert response.status_code == 422
    assert [d["field"] for d in response.json()["error"]["details"]] == [field]


async def test_create_date_range(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project
) -> None:
    response = await client.post(
        tasks_url(web),
        headers=admin["headers"],
        json={"title": "A", "start_date": "2026-12-05", "end_date": "2026-12-01"},
    )

    assert response.status_code == 422
    assert "end_date can't be before start_date" in response.text


# ── TSK-3 details ─────────────────────────────────────────────────────────────


async def test_task_details_in_one_query(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    ravi = await project_user(db_session, acme, web, "Ravi Kumar")
    created = await new_task(client, admin["headers"], web, assignee_id=ravi, description="Steps")
    task = await db_session.scalar(select(Task).where(Task.project_id == web.id))
    for _ in range(3):
        await make_task_comment(db_session, task=task)

    with count_queries(engine) as queries:
        response = await client.get(tasks_url(web, "/1"), headers=admin["headers"])

    body = response.json()
    assert body["comment_count"] == 3
    assert body["assignee"] == {"id": ravi, "name": "Ravi Kumar"}
    assert body["description"] == "Steps"
    assert body["created_at"] == created["created_at"]
    # user + project + permissions + one SELECT (space, status, users, count joined)
    assert queries.count == 4, str(queries)


async def test_unknown_tasks_are_not_found(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    other = await make_project(db_session, organization=acme)
    other_number = (await new_project(client, admin["headers"], task_prefix="APP"))["public_id"]
    other_project = await db_session.scalar(
        select(Project).where(Project.public_id == other_number)
    )
    await new_task(client, admin["headers"], other_project)  # APP-1 exists, WEB-1 doesn't

    assert (await client.get(tasks_url(web, "/1"), headers=admin["headers"])).status_code == 404
    assert (await client.get(tasks_url(web, "/0"), headers=admin["headers"])).status_code == 422
    assert (
        await client.get(tasks_url(web, "/2147483648"), headers=admin["headers"])
    ).status_code == 422
    assert (await client.get(tasks_url(web, "/abc"), headers=admin["headers"])).status_code == 422
    assert (
        await client.get(f"/projects/{other.public_id}/tasks/1", headers=admin["headers"])
    ).status_code == 404


# ── TSK-1 list ────────────────────────────────────────────────────────────────


@pytest.fixture
async def backlog(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> dict[str, Any]:
    """Five WEB tasks with different spaces, statuses, assignees, priorities, dates."""
    headers = admin["headers"]
    ids = await status_ids(db_session, web)
    frontend = (await make_space(db_session, project=web, name="Frontend", position=1)).id
    ravi = await project_user(db_session, acme, web, "Ravi")
    meera = await project_user(db_session, acme, web, "Meera")
    await new_task(
        client,
        headers,
        web,
        title="Fix login redirect",
        priority="high",
        assignee_id=ravi,
        end_date="2026-12-10",
    )
    await new_task(client, headers, web, title="Write docs", priority="low", space_id=frontend)
    await new_task(
        client,
        headers,
        web,
        title="Login page styling",
        priority="urgent",
        status_id=ids["In Progress"],
        assignee_id=meera,
        space_id=frontend,
        end_date="2026-12-01",
    )
    await new_task(
        client, headers, web, title="Release 1.0", status_id=ids["Done"], assignee_id=ravi
    )
    await new_task(client, headers, web, title="50%_off banner", priority="high")
    return {"ravi": ravi, "meera": meera, "frontend": frontend, "ids": ids}


async def test_list_shape_and_default_order(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project, backlog: dict
) -> None:
    response = await client.get(tasks_url(web), headers=admin["headers"])

    body = response.json()
    # Default: newest first. One test transaction gives all rows the same
    # created_at, so the number (the tie-breaker) decides.
    assert [t["key"] for t in body["items"]] == ["WEB-5", "WEB-4", "WEB-3", "WEB-2", "WEB-1"]
    assert "description" not in body["items"][0]
    assert set(body["items"][0]) == {
        "number", "key", "title", "priority", "space", "status", "assignee",
        "start_date", "end_date", "created_at", "updated_at",
    }  # fmt: skip
    assert body["pagination"]["total_items"] == 5


async def test_list_filters(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project, backlog: dict
) -> None:
    headers, ids = admin["headers"], backlog["ids"]
    assert await listed(client, headers, web, f"?space_id={backlog['frontend']}") == [
        "WEB-3",
        "WEB-2",
    ]
    assert await listed(client, headers, web, f"?status_id={ids['Done']}") == ["WEB-4"]
    assert await listed(client, headers, web, "?category=todo") == ["WEB-5", "WEB-2", "WEB-1"]
    assert await listed(client, headers, web, f"?assignee_id={backlog['ravi']}") == [
        "WEB-4",
        "WEB-1",
    ]
    assert await listed(client, headers, web, "?unassigned=true") == ["WEB-5", "WEB-2"]
    assert await listed(client, headers, web, "?priority=high&priority=urgent") == [
        "WEB-5",
        "WEB-3",
        "WEB-1",
    ]
    assert await listed(client, headers, web, f"?category=todo&assignee_id={backlog['ravi']}") == [
        "WEB-1"
    ]
    assert await listed(client, headers, web, "?space_id=999999999") == []


async def test_list_search_by_title_or_key(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project, backlog: dict
) -> None:
    headers = admin["headers"]
    assert await listed(client, headers, web, "?q=LOGIN") == ["WEB-3", "WEB-1"]
    assert await listed(client, headers, web, "?q=web-4") == ["WEB-4"]
    assert await listed(client, headers, web, "?q=WEB-99") == []
    assert await listed(client, headers, web, "?q=50%25_") == ["WEB-5"]  # wildcards are literal
    assert await listed(client, headers, web, "?q=login&category=in_progress") == ["WEB-3"]
    too_short = await client.get(tasks_url(web, "?q=lo"), headers=headers)
    assert too_short.json()["error"]["code"] == "search_too_short"


async def test_list_sorting(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project, backlog: dict
) -> None:
    headers = admin["headers"]
    # Severity, not alphabet: urgent > high > medium > low (ties by number).
    assert await listed(client, headers, web, "?sort=priority") == [
        "WEB-3",
        "WEB-5",
        "WEB-1",
        "WEB-4",
        "WEB-2",
    ]
    assert await listed(client, headers, web, "?sort=priority&order=asc") == [
        "WEB-2",
        "WEB-4",
        "WEB-1",
        "WEB-5",
        "WEB-3",
    ]
    # Tasks without an end date come last in both directions.
    assert (await listed(client, headers, web, "?sort=end_date&order=asc"))[:2] == [
        "WEB-3",
        "WEB-1",
    ]
    assert (await listed(client, headers, web, "?sort=end_date"))[:2] == ["WEB-1", "WEB-3"]
    # Case-insensitive: "50%_off…" < "Fix login…" < "Login page…" < "Release…" < "Write…"
    assert await listed(client, headers, web, "?sort=title&order=asc") == [
        "WEB-5",
        "WEB-1",
        "WEB-3",
        "WEB-4",
        "WEB-2",
    ]
    assert await listed(client, headers, web, "?sort=number&order=asc") == [
        "WEB-1",
        "WEB-2",
        "WEB-3",
        "WEB-4",
        "WEB-5",
    ]


async def test_list_paging_and_invalid_parameters(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project, backlog: dict
) -> None:
    headers = admin["headers"]
    page_2 = await client.get(
        tasks_url(web, "?sort=number&order=asc&page=2&page_size=2"), headers=headers
    )
    assert [t["key"] for t in page_2.json()["items"]] == ["WEB-3", "WEB-4"]
    assert page_2.json()["pagination"]["total_pages"] == 3

    for query in ("?priority=critical", "?category=blocked", "?sort=assignee", "?space_id=0"):
        assert (await client.get(tasks_url(web, query), headers=headers)).status_code == 422, query
    both = await client.get(
        tasks_url(web, f"?unassigned=true&assignee_id={backlog['ravi']}"), headers=headers
    )
    assert both.status_code == 422
    assert both.json()["error"]["details"][0]["field"] == "unassigned"


async def test_list_query_count_is_constant(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    admin: dict[str, Any],
    web: Project,
    backlog: dict,
) -> None:
    for _ in range(15):
        await new_task(client, admin["headers"], web, assignee_id=backlog["meera"])

    with count_queries(engine) as queries:
        response = await client.get(tasks_url(web, "?page_size=50"), headers=admin["headers"])

    assert len(response.json()["items"]) == 20
    # user + project + permissions + COUNT + page: no per-task queries (no N+1)
    assert queries.count == 5, str(queries)


# ── TSK-4 edit form ───────────────────────────────────────────────────────────


async def test_save_edit_form(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    ravi = await project_user(db_session, acme, web, "Ravi")
    doing = (await status_ids(db_session, web))["In Progress"]
    task = await new_task(
        client, admin["headers"], web, description="Old", assignee_id=ravi, end_date="2026-12-10"
    )

    response = await client.patch(
        tasks_url(web, "/1"),
        headers=admin["headers"],
        json={
            "title": "Fix login redirect on Safari",
            "status_id": doing,
            "priority": "urgent",
            "assignee_id": None,
            "description": "  ",
            "end_date": "2026-12-12",
            "expected_updated_at": task["updated_at"],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "Fix login redirect on Safari"
    assert (body["status"]["name"], body["priority"], body["assignee"]) == (
        "In Progress",
        "urgent",
        None,
    )
    assert (body["description"], body["end_date"]) == (None, "2026-12-12")
    assert body["space"] == task["space"]  # not sent, not changed
    assert body["updated_at"] > task["updated_at"]


async def test_stale_edit_returns_the_current_task(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project
) -> None:
    task = await new_task(client, admin["headers"], web)
    first = await client.patch(
        tasks_url(web, "/1"),
        headers=admin["headers"],
        json={"title": "First", "expected_updated_at": task["updated_at"]},
    )

    second = await client.patch(
        tasks_url(web, "/1"),
        headers=admin["headers"],
        json={"title": "Second", "expected_updated_at": task["updated_at"]},
    )

    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "stale_update"
    assert error["details"]["current"]["title"] == "First"
    assert error["details"]["current"]["updated_at"] == first.json()["updated_at"]
    assert "id" not in error["details"]["current"]


@pytest.mark.parametrize(
    "changes",
    [
        {"title": None},
        {"space_id": None},
        {"status_id": None},
        {"priority": None},
        {"number": 5},
        {},  # expected_updated_at is required
    ],
)
async def test_invalid_edit_body(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project, changes: dict
) -> None:
    task = await new_task(client, admin["headers"], web)
    body = changes if changes == {} else {"expected_updated_at": task["updated_at"]} | changes

    response = await client.patch(tasks_url(web, "/1"), headers=admin["headers"], json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_edit_checks_references_and_stored_dates(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    task = await new_task(client, admin["headers"], web, start_date="2026-12-10")
    other = await make_project(db_session, organization=acme)
    foreign_status = (await make_task_status(db_session, project=other)).id
    no_role = (await make_user(db_session, organization=acme)).id
    stamp = {"expected_updated_at": task["updated_at"]}

    for body, field in (
        ({"status_id": foreign_status}, "status_id"),
        ({"assignee_id": no_role}, "assignee_id"),
        ({"end_date": "2026-12-01"}, "end_date"),  # before the STORED start_date
    ):
        response = await client.patch(
            tasks_url(web, "/1"), headers=admin["headers"], json=stamp | body
        )
        assert response.status_code == 422, body
        assert response.json()["error"]["details"][0]["field"] == field
    cleared = await client.patch(
        tasks_url(web, "/1"),
        headers=admin["headers"],
        json=stamp | {"start_date": None, "end_date": "2026-12-01"},
    )
    assert cleared.status_code == 200


# ── TSK-8 delete ──────────────────────────────────────────────────────────────


async def test_delete_task_and_number_is_not_reused(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], web: Project
) -> None:
    await new_task(client, admin["headers"], web)
    await new_task(client, admin["headers"], web)
    task_id = await db_session.scalar(
        select(Task.id).where(Task.project_id == web.id, Task.number == 2)
    )
    await make_task_comment(db_session, task=await db_session.get(Task, task_id))

    response = await client.delete(tasks_url(web, "/2"), headers=admin["headers"])

    assert response.status_code == 204
    comments = await db_session.scalar(
        select(func.count()).select_from(TaskComment).where(TaskComment.task_id == task_id)
    )
    assert comments == 0  # CASCADE
    assert (await client.delete(tasks_url(web, "/2"), headers=admin["headers"])).status_code == 404
    assert (await new_task(client, admin["headers"], web))["key"] == "WEB-3"  # WEB-2 stays retired


# ── Permissions and archived projects ─────────────────────────────────────────


async def test_task_permissions(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    task = await new_task(client, admin["headers"], web)
    viewer = await member(db_session, acme, {"tasks": "view"}, web)
    creator = await member(db_session, acme, {"tasks": "create"}, web)
    editor = await member(db_session, acme, {"tasks": "edit"}, web)
    deleter = await member(db_session, acme, {"tasks": "delete"}, web)
    statuses_only = await member(db_session, acme, {"task_statuses": "view"}, web)
    nobody = await outsider(db_session, acme)
    edit = {"title": "X", "expected_updated_at": task["updated_at"]}

    assert (await client.get(tasks_url(web), headers=viewer)).status_code == 200
    assert (await client.get(tasks_url(web, "/1"), headers=viewer)).status_code == 200
    assert (await client.get(tasks_url(web), headers=statuses_only)).status_code == 403
    assert (await client.get(tasks_url(web), headers=nobody)).status_code == 404
    assert (
        await client.post(tasks_url(web), headers=viewer, json={"title": "A"})
    ).status_code == 403
    created = await client.post(tasks_url(web), headers=creator, json={"title": "A"})
    assert created.status_code == 201
    assert created.json()["created_by"]["name"] != "Asha Rao"  # the caller, from the token
    assert (await client.patch(tasks_url(web, "/1"), headers=viewer, json=edit)).status_code == 403
    assert (await client.patch(tasks_url(web, "/1"), headers=editor, json=edit)).status_code == 200
    assert (await client.delete(tasks_url(web, "/1"), headers=editor)).status_code == 403
    assert (await client.delete(tasks_url(web, "/1"), headers=deleter)).status_code == 204


async def test_archived_project_tasks_are_read_only(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], web: Project
) -> None:
    task = await new_task(client, admin["headers"], web)
    await db_session.execute(update(Project).where(Project.id == web.id).values(status="archived"))
    headers = admin["headers"]

    writes = [
        await client.post(tasks_url(web), headers=headers, json={"title": "A"}),
        await client.patch(
            tasks_url(web, "/1"),
            headers=headers,
            json={"title": "X", "expected_updated_at": task["updated_at"]},
        ),
        await client.delete(tasks_url(web, "/1"), headers=headers),
    ]

    assert [w.json()["error"]["code"] for w in writes] == ["project_archived"] * 3
    assert (await client.get(tasks_url(web, "/1"), headers=headers)).status_code == 200
    assert await listed(client, headers, web) == ["WEB-1"]


# ── Concurrency: real, separate transactions ─────────────────────────────────

RACE_SLUG = "race-tasks"


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
            # Tasks first: their status and creator foreign keys don't cascade.
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
    async with AsyncSession(engine, expire_on_commit=False) as setup:
        organization = await setup.scalar(
            select(Organization).where(Organization.slug == RACE_SLUG)
        )
        project_row = await setup.scalar(
            select(Project).where(Project.public_id == project["public_id"])
        )
        ravi = await project_user(setup, organization, project_row, "Ravi")
        await setup.commit()
    return {
        "headers": headers,
        "tasks": f"/projects/{project['public_id']}/tasks",
        "public_id": project["public_id"],
        "ravi": ravi,
    }


async def test_simultaneous_creates_get_unique_numbers(
    real_client: httpx.AsyncClient, race: dict[str, Any]
) -> None:
    created: list[dict] = []

    async def create(n: int) -> None:
        response = await real_client.post(
            race["tasks"], headers=race["headers"], json={"title": f"Task {n}"}
        )
        assert response.status_code == 201, response.text
        created.append(response.json())

    async with anyio.create_task_group() as group:
        for n in range(10):
            group.start_soon(create, n)

    assert sorted(task["number"] for task in created) == list(range(1, 11))
    assert len({task["key"] for task in created}) == 10


async def test_member_removal_unassigns_a_task_being_created(
    real_client: httpx.AsyncClient, engine: AsyncEngine, race: dict[str, Any]
) -> None:
    """A task is being created for Ravi while Ravi is removed from the project.
    The removal waits for the create (FOR SHARE on his role row), then
    unassigns the new task too (A-7): no task is left with an outsider."""
    responses: list[httpx.Response] = []

    async def remove_ravi() -> None:
        responses.append(
            await real_client.delete(
                f"/projects/{race['public_id']}/members/{race['ravi']}", headers=race["headers"]
            )
        )

    async with AsyncSession(engine) as creator:
        # What TSK-2 does for an assignee: lock his access rows, then insert.
        await creator.execute(
            select(UserRole.id).where(UserRole.user_id == race["ravi"]).with_for_update(read=True)
        )
        project_id = await creator.scalar(
            select(Project.id).where(Project.public_id == race["public_id"])
        )
        space_id = await creator.scalar(select(Space.id).where(Space.project_id == project_id))
        status_id = await creator.scalar(
            select(TaskStatus.id).where(TaskStatus.project_id == project_id).limit(1)
        )
        creator.add(
            Task(
                project_id=project_id, number=1, title="For Ravi", space_id=space_id,
                status_id=status_id, assignee_id=race["ravi"], created_by=race["ravi"],
            )
        )  # fmt: skip
        await creator.flush()
        async with anyio.create_task_group() as group:
            group.start_soon(remove_ravi)
            await anyio.sleep(0.5)
            assert responses == []  # waiting on our share lock
            await creator.commit()

    assert responses[0].status_code == 204
    async with AsyncSession(engine) as check:
        assignee = await check.scalar(
            select(Task.assignee_id).where(Task.project_id == project_id, Task.number == 1)
        )
    assert assignee is None


async def test_create_for_a_member_being_removed_is_refused(
    real_client: httpx.AsyncClient, engine: AsyncEngine, race: dict[str, Any]
) -> None:
    """The other order: Ravi's removal is in progress. TSK-2's share lock
    waits for it, then finds he has no access: 422, no task assigned to him."""
    responses: list[httpx.Response] = []

    async def create() -> None:
        responses.append(
            await real_client.post(
                race["tasks"],
                headers=race["headers"],
                json={"title": "For Ravi", "assignee_id": race["ravi"]},
            )
        )

    async with AsyncSession(engine) as remover:
        await remover.execute(
            delete(UserRole).where(
                UserRole.user_id == race["ravi"], UserRole.project_id.is_not(None)
            )
        )
        async with anyio.create_task_group() as group:
            group.start_soon(create)
            await anyio.sleep(0.5)
            assert responses == []
            await remover.commit()

    assert responses[0].status_code == 422
    assert responses[0].json()["error"]["details"][0]["field"] == "assignee_id"
