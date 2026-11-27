"""M7b: PRJ-4 update, PRJ-5 archive, PRJ-6 unarchive."""

from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.session import get_session
from app.main import create_app
from app.models import Organization, Project, Role, User
from tests.api.project_helpers import REGISTER, bearer, member, new_project, outsider
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
async def created(client: httpx.AsyncClient, admin: dict[str, Any]) -> dict:
    return await new_project(
        client,
        admin["headers"],
        description="New marketing site",
        start_date="2026-12-01",
        end_date="2027-03-31",
    )


async def load(session: AsyncSession, public_id: str) -> Project:
    return await session.scalar(select(Project).where(Project.public_id == public_id))


async def patch(client: httpx.AsyncClient, headers: dict, project: dict, **changes: Any):
    body = {"expected_updated_at": project["updated_at"]} | changes
    return await client.patch(f"/projects/{project['public_id']}", headers=headers, json=body)


# ── PRJ-4 PATCH /projects/{public_id} ─────────────────────────────────────────


async def test_update_changes_only_the_fields_sent(
    client: httpx.AsyncClient, admin: dict[str, Any], created: dict
) -> None:
    response = await patch(
        client, admin["headers"], created, name=" Website 2.0 ", status="on_hold", end_date=None
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["name"], body["status"], body["end_date"]) == ("Website 2.0", "on_hold", None)
    # Not sent, so unchanged:
    assert (body["description"], body["start_date"]) == ("New marketing site", "2026-12-01")
    assert (body["task_prefix"], body["public_id"]) == ("WEB", created["public_id"])
    assert body["updated_at"] > created["updated_at"]
    assert body["task_counts"]["total"] == 0 and "my_permissions" in body


async def test_blank_description_clears_it(
    client: httpx.AsyncClient, admin: dict[str, Any], created: dict
) -> None:
    response = await patch(client, admin["headers"], created, description="   ")

    assert response.json()["description"] is None


async def test_update_query_count(
    client: httpx.AsyncClient, engine: AsyncEngine, admin: dict[str, Any], created: dict
) -> None:
    with count_queries(engine) as queries:
        response = await patch(client, admin["headers"], created, status="completed")

    assert response.status_code == 200
    # user + project + permissions + project lock (A-9) + UPDATE…RETURNING
    # + task counts (BEGIN/COMMIT/SAVEPOINT aren't counted)
    assert queries.count == 6, str(queries)


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"task_prefix": "NEW"}, "task_prefix"),  # can't be changed (A-8)
        ({"status": "archived"}, "status"),  # only through PRJ-5
        ({"status": None}, "status"),
        ({"name": None}, "name"),
        ({"name": "x" * 151}, "name"),
        ({"expected_updated_at": None}, "expected_updated_at"),
        ({"expected_updated_at": "2026-11-16T09:00:00"}, "expected_updated_at"),  # no timezone
    ],
)
async def test_invalid_update_body(
    client: httpx.AsyncClient, admin: dict[str, Any], created: dict, changes: dict, field: str
) -> None:
    response = await patch(client, admin["headers"], created, **changes)

    assert response.status_code == 422
    assert [d["field"] for d in response.json()["error"]["details"]] == [field]


@pytest.mark.parametrize(
    "changes",
    [
        {"start_date": "2027-02-01", "end_date": "2027-01-01"},  # both sent: the schema
        {"end_date": "2026-11-30"},  # before the STORED start_date: the service
        {"start_date": "2027-04-01"},  # after the STORED end_date: the service
    ],
)
async def test_end_date_before_start_date(
    client: httpx.AsyncClient, admin: dict[str, Any], created: dict, changes: dict
) -> None:
    response = await patch(client, admin["headers"], created, **changes)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "end_date can't be before start_date" in response.text


async def test_clearing_one_date_makes_any_other_date_valid(
    client: httpx.AsyncClient, admin: dict[str, Any], created: dict
) -> None:
    response = await patch(
        client, admin["headers"], created, start_date=None, end_date="2020-01-01"
    )

    assert response.status_code == 200
    assert (response.json()["start_date"], response.json()["end_date"]) == (None, "2020-01-01")


async def test_stale_update_returns_the_current_project(
    client: httpx.AsyncClient, admin: dict[str, Any], created: dict
) -> None:
    first = await patch(client, admin["headers"], created, name="First save")
    second = await patch(client, admin["headers"], created, name="Second save")  # old copy

    assert first.status_code == 200
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "stale_update"
    assert error["details"]["current"]["name"] == "First save"
    assert error["details"]["current"]["updated_at"] == first.json()["updated_at"]
    assert "id" not in error["details"]["current"]  # never the internal id


async def test_empty_update_just_confirms_the_version(
    client: httpx.AsyncClient, admin: dict[str, Any], created: dict
) -> None:
    current = await patch(client, admin["headers"], created)
    stale = await patch(client, admin["headers"], created | {"updated_at": "2026-01-01T00:00:00Z"})

    assert current.status_code == 200
    assert current.json()["updated_at"] == created["updated_at"]  # nothing was written
    assert stale.status_code == 409


async def test_update_permissions(
    client: httpx.AsyncClient, db_session: AsyncSession, acme: Organization, created: dict
) -> None:
    project = await load(db_session, created["public_id"])
    viewer = await member(db_session, acme, {"projects": "view"}, project)
    editor = await member(db_session, acme, {"projects": "view,edit"}, project)
    nobody = await outsider(db_session, acme)

    assert (await patch(client, nobody, created, name="X")).status_code == 404
    assert (await patch(client, viewer, created, name="X")).status_code == 403
    assert (await patch(client, editor, created, name="X")).status_code == 200


# ── PRJ-5 archive / PRJ-6 unarchive ───────────────────────────────────────────


async def test_archive_and_unarchive_round_trip(
    client: httpx.AsyncClient, admin: dict[str, Any], created: dict
) -> None:
    url = f"/projects/{created['public_id']}"
    on_hold = (await patch(client, admin["headers"], created, status="on_hold")).json()

    archived = await client.post(f"{url}/archive", headers=admin["headers"])
    again = await client.post(f"{url}/archive", headers=admin["headers"])

    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    assert archived.json()["updated_at"] > on_hold["updated_at"]
    assert again.status_code == 200  # a repeated click is harmless…
    assert again.json()["updated_at"] == archived.json()["updated_at"]  # …and changes nothing
    # Read-only now, but still readable:
    blocked = await patch(client, admin["headers"], archived.json(), name="X")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "project_archived"
    assert (await client.get(url, headers=admin["headers"])).status_code == 200

    restored = await client.post(f"{url}/unarchive", headers=admin["headers"])
    restored_again = await client.post(f"{url}/unarchive", headers=admin["headers"])

    assert restored.status_code == 200
    assert restored.json()["status"] == "active"  # back to active, not on_hold
    assert restored_again.status_code == 200
    assert restored_again.json()["updated_at"] == restored.json()["updated_at"]
    assert (await patch(client, admin["headers"], restored.json(), name="Y")).status_code == 200


async def test_archive_needs_projects_delete(
    client: httpx.AsyncClient, db_session: AsyncSession, acme: Organization, created: dict
) -> None:
    project = await load(db_session, created["public_id"])
    editor = await member(db_session, acme, {"projects": "view,edit"}, project)
    deleter = await member(db_session, acme, {"projects": "view,delete"}, project)
    nobody = await outsider(db_session, acme)
    url = f"/projects/{created['public_id']}"

    assert (await client.post(f"{url}/archive", headers=nobody)).status_code == 404
    assert (await client.post(f"{url}/archive", headers=editor)).status_code == 403
    assert (await client.post(f"{url}/archive", headers=deleter)).status_code == 200
    # Unarchive works on the archived project (the guard allows it) — same rules.
    assert (await client.post(f"{url}/unarchive", headers=editor)).status_code == 403
    assert (await client.post(f"{url}/unarchive", headers=deleter)).status_code == 200


async def test_archived_projects_leave_the_default_list(
    client: httpx.AsyncClient, admin: dict[str, Any], created: dict
) -> None:
    await client.post(f"/projects/{created['public_id']}/archive", headers=admin["headers"])

    default = await client.get("/projects", headers=admin["headers"])
    archived = await client.get("/projects?status=archived", headers=admin["headers"])

    assert default.json()["items"] == []
    assert [p["public_id"] for p in archived.json()["items"]] == [created["public_id"]]


async def test_archive_keeps_task_counts(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], created: dict
) -> None:
    await db_session.execute(
        update(Project).where(Project.public_id == created["public_id"]).values(status="completed")
    )

    body = (
        await client.post(f"/projects/{created['public_id']}/archive", headers=admin["headers"])
    ).json()

    assert body["task_counts"] == {"todo": 0, "in_progress": 0, "done": 0, "total": 0}
    assert set(body["my_permissions"]) >= {"projects", "tasks"}


# ── Concurrency: real, separate transactions ─────────────────────────────────

RACE_SLUG = "race-projects"


@pytest.fixture
async def real_client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """Each request gets its own session and really commits (no shared test
    transaction), like two browser tabs. Everything is deleted afterwards."""
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
            # Spaces and statuses go with their project (ON DELETE CASCADE).
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
    return {"headers": headers, "project": await new_project(real_client, headers)}


async def test_two_simultaneous_saves_one_wins(
    real_client: httpx.AsyncClient, race: dict[str, Any]
) -> None:
    results: dict[str, int] = {}

    async def save(name: str) -> None:
        response = await patch(real_client, race["headers"], race["project"], name=name)
        results[name] = response.status_code

    async with anyio.create_task_group() as group:
        group.start_soon(save, "Tab A")
        group.start_soon(save, "Tab B")

    assert sorted(results.values()) == [200, 409]
    winner = next(name for name, code in results.items() if code == 200)
    url = f"/projects/{race['project']['public_id']}"
    assert (await real_client.get(url, headers=race["headers"])).json()["name"] == winner


async def test_two_simultaneous_archives_both_succeed(
    real_client: httpx.AsyncClient, race: dict[str, Any]
) -> None:
    url = f"/projects/{race['project']['public_id']}/archive"
    responses = []

    async def archive() -> None:
        responses.append(await real_client.post(url, headers=race["headers"]))

    async with anyio.create_task_group() as group:
        group.start_soon(archive)
        group.start_soon(archive)

    assert [r.status_code for r in responses] == [200, 200]
    assert {r.json()["status"] for r in responses} == {"archived"}
    assert len({r.json()["updated_at"] for r in responses}) == 1  # written once


async def test_save_racing_an_archive_is_refused(
    real_client: httpx.AsyncClient, engine: AsyncEngine, race: dict[str, Any]
) -> None:
    """Another user archives the project (not yet committed) while a save is on
    its way. The save's guard still sees an active project, its UPDATE waits for
    the archive's row lock, then finds the project archived: 409 project_archived,
    and the name is not changed."""
    project = race["project"]

    async with AsyncSession(engine) as archiver:
        await archiver.execute(
            update(Project)
            .where(Project.public_id == project["public_id"])
            .values(status="archived")
        )
        async with anyio.create_task_group() as group:
            responses: list[httpx.Response] = []

            async def save() -> None:
                responses.append(
                    await patch(real_client, race["headers"], project, name="Too late")
                )

            group.start_soon(save)
            await anyio.sleep(0.5)  # the save is now blocked on the row lock
            await archiver.commit()

    assert responses[0].status_code == 409
    assert responses[0].json()["error"]["code"] == "project_archived"
    current = (
        await real_client.get(f"/projects/{project['public_id']}", headers=race["headers"])
    ).json()
    assert (current["name"], current["status"]) == ("Website Revamp", "archived")
