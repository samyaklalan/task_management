"""M9: TEAM-1 to TEAM-8, plus the archive lock every project-scoped write uses."""

from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.session import get_session
from app.main import create_app
from app.models import Organization, Project, Role, Team, TeamMember, User, UserRole
from app.repositories import project as project_repository
from tests.api.project_helpers import REGISTER, bearer, member, new_project, outsider
from tests.factories import make_organization, make_project, make_role, make_team, make_user
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


def url(project: Project, team_id: int | None = None, tail: str = "") -> str:
    base = f"/projects/{project.public_id}/teams"
    return (base if team_id is None else f"{base}/{team_id}") + tail


async def project_member(
    session: AsyncSession, organization: Organization, project: Project, name: str, **values: Any
) -> int:
    """A user with a role in ``project``; returns the user id (a plain int, so
    it survives a rollback in the shared test session)."""
    user = await make_user(session, organization=organization, name=name, **values)
    role = await make_role(session, organization=organization)
    session.add(UserRole(user_id=user.id, role_id=role.id, project_id=project.id))
    await session.flush()
    return user.id


async def archive(session: AsyncSession, project: Project) -> None:
    await session.execute(update(Project).where(Project.id == project.id).values(status="archived"))


# ── TEAM-1 to TEAM-5 ──────────────────────────────────────────────────────────


async def test_create_and_get_team(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project
) -> None:
    created = await client.post(
        url(web), headers=admin["headers"], json={"name": " Frontend ", "description": "UI squad"}
    )

    assert created.status_code == 201
    body = created.json()
    assert (body["name"], body["description"], body["member_count"]) == ("Frontend", "UI squad", 0)
    assert set(body) == {"id", "name", "description", "member_count", "created_at", "updated_at"}
    fetched = await client.get(url(web, body["id"]), headers=admin["headers"])
    assert fetched.json() == body
    # Names don't have to be unique.
    assert (
        await client.post(url(web), headers=admin["headers"], json={"name": "Frontend"})
    ).status_code == 201


@pytest.mark.parametrize(
    "body", [{}, {"name": "  "}, {"name": "x" * 101}, {"name": "A", "project_id": 1}]
)
async def test_invalid_team_body(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project, body: dict
) -> None:
    assert (await client.post(url(web), headers=admin["headers"], json=body)).status_code == 422


async def test_list_teams_with_member_counts(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    other_project = await make_project(db_session, organization=acme)
    await make_team(db_session, project=other_project, name="Backend elsewhere")
    teams = {
        name: await make_team(db_session, project=web, name=name)
        for name in ("frontend", "Backend", "QA")
    }
    for n in range(3):
        user_id = await project_member(db_session, acme, web, f"Dev {n}")
        db_session.add(TeamMember(team_id=teams["Backend"].id, user_id=user_id))
    user_id = await project_member(db_session, acme, web, "Tester")
    db_session.add(TeamMember(team_id=teams["QA"].id, user_id=user_id))
    await db_session.flush()

    with count_queries(engine) as queries:
        response = await client.get(url(web), headers=admin["headers"])

    assert [(t["name"], t["member_count"]) for t in response.json()["items"]] == [
        ("Backend", 3),
        ("frontend", 0),
        ("QA", 1),
    ]
    # user + project + permissions + COUNT + page (counts in the same SELECT)
    assert queries.count == 5, str(queries)

    by_created = await client.get(url(web) + "?sort=created_at", headers=admin["headers"])
    assert [t["name"] for t in by_created.json()["items"]] == ["frontend", "Backend", "QA"]
    search = await client.get(url(web) + "?q=END", headers=admin["headers"])
    assert [t["name"] for t in search.json()["items"]] == ["Backend", "frontend"]
    assert (await client.get(url(web) + "?q=ab", headers=admin["headers"])).status_code == 422
    assert (await client.get(url(web) + "?sort=size", headers=admin["headers"])).status_code == 422


async def test_teams_of_other_projects_are_not_found(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    elsewhere = await make_team(
        db_session, project=await make_project(db_session, organization=acme)
    )
    headers = admin["headers"]
    expected = {"expected_updated_at": "2026-01-01T00:00:00Z", "name": "X"}

    assert (await client.get(url(web, elsewhere.id), headers=headers)).status_code == 404
    assert (
        await client.patch(url(web, elsewhere.id), headers=headers, json=expected)
    ).status_code == 404
    assert (await client.delete(url(web, elsewhere.id), headers=headers)).status_code == 404
    assert (
        await client.get(url(web, elsewhere.id, "/members"), headers=headers)
    ).status_code == 404
    assert (await client.get(url(web, 999_999_999), headers=headers)).status_code == 404


async def test_update_team(client: httpx.AsyncClient, admin: dict[str, Any], web: Project) -> None:
    team = (
        await client.post(
            url(web), headers=admin["headers"], json={"name": "Frontend", "description": "UI"}
        )
    ).json()

    renamed = await client.patch(
        url(web, team["id"]),
        headers=admin["headers"],
        json={"name": "Web UI", "expected_updated_at": team["updated_at"]},
    )
    stale = await client.patch(
        url(web, team["id"]),
        headers=admin["headers"],
        json={"description": None, "expected_updated_at": team["updated_at"]},
    )

    assert renamed.status_code == 200
    assert (renamed.json()["name"], renamed.json()["description"]) == ("Web UI", "UI")
    assert renamed.json()["updated_at"] > team["updated_at"]
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_update"
    assert stale.json()["error"]["details"]["current"]["name"] == "Web UI"

    cleared = await client.patch(
        url(web, team["id"]),
        headers=admin["headers"],
        json={"description": None, "expected_updated_at": renamed.json()["updated_at"]},
    )
    assert cleared.json()["description"] is None
    null_name = await client.patch(
        url(web, team["id"]),
        headers=admin["headers"],
        json={"name": None, "expected_updated_at": cleared.json()["updated_at"]},
    )
    assert null_name.status_code == 422


async def test_delete_team_keeps_users(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    team = await make_team(db_session, project=web)
    team_id = team.id
    user_id = await project_member(db_session, acme, web, "Ravi")
    db_session.add(TeamMember(team_id=team_id, user_id=user_id))
    await db_session.flush()

    response = await client.delete(url(web, team_id), headers=admin["headers"])

    assert response.status_code == 204
    assert await db_session.get(Team, team_id, populate_existing=True) is None
    memberships = await db_session.scalar(
        select(func.count()).select_from(TeamMember).where(TeamMember.team_id == team_id)
    )
    assert memberships == 0  # CASCADE
    assert await db_session.get(User, user_id) is not None
    assert (await client.delete(url(web, team_id), headers=admin["headers"])).status_code == 404


async def test_team_permissions(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    acme: Organization,
    web: Project,
) -> None:
    team = await make_team(db_session, project=web)
    viewer = await member(db_session, acme, {"teams": "view"}, web)
    creator = await member(db_session, acme, {"teams": "create"}, web)
    deleter = await member(db_session, acme, {"teams": "delete"}, web)
    tasks_only = await member(db_session, acme, {"tasks": "view"}, web)
    nobody = await outsider(db_session, acme)

    assert (await client.get(url(web), headers=viewer)).status_code == 200
    assert (await client.get(url(web), headers=tasks_only)).status_code == 403
    assert (await client.get(url(web), headers=nobody)).status_code == 404
    assert (await client.post(url(web), headers=viewer, json={"name": "A"})).status_code == 403
    assert (await client.post(url(web), headers=creator, json={"name": "A"})).status_code == 201
    assert (await client.delete(url(web, team.id), headers=creator)).status_code == 403
    assert (await client.delete(url(web, team.id), headers=deleter)).status_code == 204


async def test_archived_project_teams_are_read_only(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    team = await make_team(db_session, project=web)
    user_id = await project_member(db_session, acme, web, "Ravi")
    await archive(db_session, web)
    headers = admin["headers"]
    stamp = {"name": "X", "expected_updated_at": team.updated_at.isoformat()}

    writes = [
        await client.post(url(web), headers=headers, json={"name": "A"}),
        await client.patch(url(web, team.id), headers=headers, json=stamp),
        await client.delete(url(web, team.id), headers=headers),
        await client.post(
            url(web, team.id, "/members"), headers=headers, json={"user_ids": [user_id]}
        ),
        await client.delete(url(web, team.id, f"/members/{user_id}"), headers=headers),
    ]

    assert [w.status_code for w in writes] == [409] * 5
    assert {w.json()["error"]["code"] for w in writes} == {"project_archived"}
    assert (await client.get(url(web, team.id), headers=headers)).status_code == 200
    assert (await client.get(url(web, team.id, "/members"), headers=headers)).status_code == 200


# ── TEAM-6 to TEAM-8 ──────────────────────────────────────────────────────────


async def test_add_list_and_remove_members(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    team = (await client.post(url(web), headers=admin["headers"], json={"name": "Frontend"})).json()
    ravi = await project_member(db_session, acme, web, "Ravi Kumar")
    meera = await project_member(db_session, acme, web, "meera")
    asha = await db_session.scalar(
        select(User.id).where(User.email == REGISTER["email"])
    )  # org-wide
    members_url = url(web, team["id"], "/members")

    first = await client.post(members_url, headers=admin["headers"], json={"user_ids": [ravi]})
    second = await client.post(
        members_url, headers=admin["headers"], json={"user_ids": [meera, ravi, asha, meera]}
    )

    assert first.status_code == 200
    assert first.json() == {"added": [ravi], "skipped": []}
    assert second.json() == {"added": [meera, asha], "skipped": [ravi]}  # duplicates count once

    with count_queries(engine) as queries:
        listed = await client.get(members_url, headers=admin["headers"])
    assert [i["user"]["name"] for i in listed.json()["items"]] == [
        "Asha Rao",
        "meera",
        "Ravi Kumar",
    ]
    assert set(listed.json()["items"][0]) == {"user", "joined_at"}
    assert set(listed.json()["items"][0]["user"]) == {"id", "name"}
    # user + project + permissions + team + COUNT + page (names joined in)
    assert queries.count == 6, str(queries)
    # One test transaction gives every row the same created_at, so set one apart.
    await db_session.execute(
        update(TeamMember)
        .where(TeamMember.user_id == meera)
        .values(created_at=func.now() - text("interval '1 hour'"))
    )
    by_joined = await client.get(members_url + "?sort=joined_at", headers=admin["headers"])
    assert [i["user"]["id"] for i in by_joined.json()["items"]][0] == meera
    team_now = (await client.get(url(web, team["id"]), headers=admin["headers"])).json()
    assert team_now["member_count"] == 3

    removed = await client.delete(f"{members_url}/{ravi}", headers=admin["headers"])
    again = await client.delete(f"{members_url}/{ravi}", headers=admin["headers"])
    assert removed.status_code == 204
    assert again.status_code == 404
    listed = await client.get(members_url, headers=admin["headers"])
    assert [i["user"]["name"] for i in listed.json()["items"]] == ["Asha Rao", "meera"]


async def test_added_users_must_have_project_access(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    team = await make_team(db_session, project=web)
    team_id = team.id
    ok = await project_member(db_session, acme, web, "Ravi")
    other_project = await make_project(db_session, organization=acme)
    elsewhere = await project_member(db_session, acme, other_project, "Elsewhere")
    inactive = await project_member(db_session, acme, web, "Gone", is_active=False)
    no_role = (await make_user(db_session, organization=acme)).id
    foreign_org = await make_organization(db_session)
    foreign_user = await make_user(db_session, organization=foreign_org)
    db_session.add(
        UserRole(
            user_id=foreign_user.id,
            role_id=(await make_role(db_session, organization=foreign_org)).id,
        )
    )
    await db_session.flush()
    foreign = foreign_user.id

    response = await client.post(
        url(web, team_id, "/members"),
        headers=admin["headers"],
        json={"user_ids": [ok, elsewhere, inactive, no_role, foreign, 999_999_999]},
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_reference"
    assert error["details"][0]["field"] == "user_ids"
    assert error["details"][0]["message"].endswith(
        f"{elsewhere}, {inactive}, {no_role}, {foreign}, 999999999"
    )
    count = await db_session.scalar(
        select(func.count()).select_from(TeamMember).where(TeamMember.team_id == team_id)
    )
    assert count == 0  # all or nothing: Ravi wasn't added either


@pytest.mark.parametrize(
    "body",
    [{"user_ids": []}, {"user_ids": list(range(1, 52))}, {"user_ids": [0]}, {"user_ids": "1"}, {}],
)
async def test_invalid_add_members_body(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    web: Project,
    body: dict,
) -> None:
    team = await make_team(db_session, project=web)

    response = await client.post(url(web, team.id, "/members"), headers=admin["headers"], json=body)

    assert response.status_code == 422


async def test_member_routes_need_team_permissions(
    client: httpx.AsyncClient, db_session: AsyncSession, acme: Organization, web: Project
) -> None:
    team = await make_team(db_session, project=web)
    target = await project_member(db_session, acme, web, "Ravi")
    viewer = await member(db_session, acme, {"teams": "view"}, web)
    editor = await member(db_session, acme, {"teams": "edit"}, web)
    members_url = url(web, team.id, "/members")

    assert (await client.get(members_url, headers=viewer)).status_code == 200
    assert (
        await client.post(members_url, headers=viewer, json={"user_ids": [target]})
    ).status_code == 403
    assert (
        await client.post(members_url, headers=editor, json={"user_ids": [target]})
    ).status_code == 200
    assert (await client.delete(f"{members_url}/{target}", headers=viewer)).status_code == 403
    assert (await client.delete(f"{members_url}/{target}", headers=editor)).status_code == 204


# ── Concurrency: real, separate transactions ─────────────────────────────────

RACE_SLUG = "race-teams"


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
            # Teams, memberships, user_roles, spaces and statuses go by CASCADE.
            await cleanup.execute(delete(Project).where(Project.organization_id.in_(org_ids)))
            await cleanup.execute(delete(User).where(User.organization_id.in_(org_ids)))
            await cleanup.execute(delete(Role).where(Role.organization_id.in_(org_ids)))
            await cleanup.execute(delete(Organization).where(Organization.slug == RACE_SLUG))
            await cleanup.commit()


@pytest.fixture
async def race(real_client: httpx.AsyncClient, engine: AsyncEngine) -> dict[str, Any]:
    """An organization with a project, a team in it, and two project members."""
    signed_up = await real_client.post(
        "/auth/register", json=REGISTER | {"organization_slug": RACE_SLUG}
    )
    headers = bearer(signed_up.json()["access_token"])
    project = await new_project(real_client, headers)
    team = (
        await real_client.post(
            f"/projects/{project['public_id']}/teams", headers=headers, json={"name": "Core"}
        )
    ).json()
    async with AsyncSession(engine, expire_on_commit=False) as setup:
        organization = await setup.scalar(
            select(Organization).where(Organization.slug == RACE_SLUG)
        )
        project_row = await setup.scalar(
            select(Project).where(Project.public_id == project["public_id"])
        )
        user_ids = [
            await project_member(setup, organization, project_row, name) for name in ("A", "B")
        ]
        await setup.commit()
    return {"headers": headers, "project": project, "team": team, "user_ids": user_ids}


async def test_two_simultaneous_adds_add_each_user_once(
    real_client: httpx.AsyncClient, engine: AsyncEngine, race: dict[str, Any]
) -> None:
    members_url = f"/projects/{race['project']['public_id']}/teams/{race['team']['id']}/members"
    results: list[dict] = []

    async def add() -> None:
        response = await real_client.post(
            members_url, headers=race["headers"], json={"user_ids": race["user_ids"]}
        )
        assert response.status_code == 200
        results.append(response.json())

    async with anyio.create_task_group() as group:
        group.start_soon(add)
        group.start_soon(add)

    added = sorted(user_id for result in results for user_id in result["added"])
    assert added == sorted(race["user_ids"])  # each user added exactly once in total
    async with AsyncSession(engine) as check:
        rows = await check.scalar(
            select(func.count())
            .select_from(TeamMember)
            .where(TeamMember.team_id == race["team"]["id"])
        )
    assert rows == 2


async def test_member_removal_waits_for_a_team_add(
    real_client: httpx.AsyncClient, engine: AsyncEngine, race: dict[str, Any]
) -> None:
    """MEM-4 removes access and team memberships (A-7). If a team add for the
    same user is in progress, the removal waits for it and then removes that
    new membership too: nobody ends up in a team without project access."""
    public_id, team_id = race["project"]["public_id"], race["team"]["id"]
    user_id = race["user_ids"][0]

    async with AsyncSession(engine) as adder:
        # What TEAM-7 does inside its transaction: lock the access rows, insert.
        await adder.execute(
            select(UserRole.id).where(UserRole.user_id == user_id).with_for_update(read=True)
        )
        adder.add(TeamMember(team_id=team_id, user_id=user_id))
        await adder.flush()
        async with anyio.create_task_group() as group:
            responses: list[httpx.Response] = []

            async def remove() -> None:
                responses.append(
                    await real_client.delete(
                        f"/projects/{public_id}/members/{user_id}", headers=race["headers"]
                    )
                )

            group.start_soon(remove)
            await anyio.sleep(0.5)  # MEM-4 is now waiting on our share lock
            assert responses == []
            await adder.commit()

    assert responses[0].status_code == 204
    async with AsyncSession(engine) as check:
        still_in_team = await check.scalar(
            select(func.count())
            .select_from(TeamMember)
            .where(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
        )
    assert still_in_team == 0


async def test_archive_waits_for_a_write_in_progress(
    real_client: httpx.AsyncClient, engine: AsyncEngine, race: dict[str, Any]
) -> None:
    """A write that passed the guard holds FOR KEY SHARE on the project row;
    archiving (FOR UPDATE) waits until that write has committed."""
    public_id = race["project"]["public_id"]

    async with AsyncSession(engine) as writer:
        project_id = await writer.scalar(
            select(Project.id)
            .where(Project.public_id == public_id)
            .with_for_update(read=True, key_share=True)
        )
        async with anyio.create_task_group() as group:
            responses: list[httpx.Response] = []

            async def archive_it() -> None:
                responses.append(
                    await real_client.post(
                        f"/projects/{public_id}/archive", headers=race["headers"]
                    )
                )

            group.start_soon(archive_it)
            await anyio.sleep(0.5)
            assert responses == []  # still waiting for our write
            writer.add(Team(project_id=project_id, name="Landed before the archive"))
            await writer.commit()

    assert responses[0].status_code == 200
    assert responses[0].json()["status"] == "archived"


async def test_write_waits_for_an_archive_in_progress(
    real_client: httpx.AsyncClient, engine: AsyncEngine, race: dict[str, Any]
) -> None:
    """The other order: an archive is in progress (FOR UPDATE held, not yet
    committed). A team create waits for it, then sees the project archived."""
    public_id = race["project"]["public_id"]

    async with AsyncSession(engine) as archiver:
        await archiver.execute(
            select(Project.id).where(Project.public_id == public_id).with_for_update()
        )
        await archiver.execute(
            update(Project).where(Project.public_id == public_id).values(status="archived")
        )
        async with anyio.create_task_group() as group:
            responses: list[httpx.Response] = []

            async def create() -> None:
                responses.append(
                    await real_client.post(
                        f"/projects/{public_id}/teams",
                        headers=race["headers"],
                        json={"name": "Too late"},
                    )
                )

            group.start_soon(create)
            await anyio.sleep(0.5)
            assert responses == []
            await archiver.commit()

    assert responses[0].status_code == 409
    assert responses[0].json()["error"]["code"] == "project_archived"


async def test_team_add_waits_for_a_member_removal(
    real_client: httpx.AsyncClient, engine: AsyncEngine, race: dict[str, Any]
) -> None:
    """The other order: a member removal is in progress (role row deleted, not
    yet committed). TEAM-7's share lock waits for it, then finds no access."""
    public_id, team_id = race["project"]["public_id"], race["team"]["id"]
    user_id = race["user_ids"][0]

    async with AsyncSession(engine) as remover:
        await remover.execute(
            delete(UserRole).where(UserRole.user_id == user_id, UserRole.project_id.is_not(None))
        )
        async with anyio.create_task_group() as group:
            responses: list[httpx.Response] = []

            async def add() -> None:
                responses.append(
                    await real_client.post(
                        f"/projects/{public_id}/teams/{team_id}/members",
                        headers=race["headers"],
                        json={"user_ids": [user_id]},
                    )
                )

            group.start_soon(add)
            await anyio.sleep(0.5)
            assert responses == []  # waiting on the removal's row lock
            await remover.commit()

    assert responses[0].status_code == 422
    assert responses[0].json()["error"]["code"] == "invalid_reference"


async def test_writes_in_one_project_do_not_wait_for_each_other(
    real_client: httpx.AsyncClient, engine: AsyncEngine, race: dict[str, Any]
) -> None:
    """The guard's project lock is FOR KEY SHARE, which other writers share.
    (``with_for_update(key_share=True)`` alone would be FOR NO KEY UPDATE, an
    exclusive lock that makes every write in a project queue behind the last.)"""
    public_id = race["project"]["public_id"]

    async with AsyncSession(engine) as other_write:
        project_id = await other_write.scalar(
            select(Project.id).where(Project.public_id == public_id)
        )
        await project_repository.lock_for_child_write(other_write, project_id)  # not committed
        with anyio.fail_after(2):  # would hang until the other write commits
            response = await real_client.post(
                f"/projects/{public_id}/teams", headers=race["headers"], json={"name": "Parallel"}
            )
        await other_write.rollback()

    assert response.status_code == 201
