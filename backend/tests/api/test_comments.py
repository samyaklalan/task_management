"""M13: CMT-1 to CMT-4, task comments with the author rules of A-6."""

from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import create_app
from app.models import (
    Organization,
    Project,
    Role,
    RolePermission,
    Task,
    TaskComment,
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
    tasks_url,
)
from tests.factories import make_project, make_role, make_task, make_task_comment, make_user
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
    project = await db_session.scalar(
        select(Project).where(Project.public_id == created["public_id"])
    )
    await new_task(client, admin["headers"], project, title="Fix login redirect")  # WEB-1
    return project


def url(project: Project, tail: str = "", number: int = 1) -> str:
    return tasks_url(project, f"/{number}/comments{tail}")


async def commenter(
    session: AsyncSession, organization: Organization, project: Project, name: str, actions: str
) -> dict[str, Any]:
    """A project member whose role allows ``comments.<actions>`` (plus
    ``tasks.view``). Returns their id (a plain int) and auth header."""
    user = await make_user(session, organization=organization, name=name)
    role = await make_role(session, organization=organization)
    session.add(RolePermission(role_id=role.id, feature="tasks", view_perm=True))
    session.add(
        RolePermission(
            role_id=role.id,
            feature="comments",
            **{f"{action}_perm": True for action in actions.split(",")},
        )
    )
    session.add(UserRole(user_id=user.id, role_id=role.id, project_id=project.id))
    await session.flush()
    return {"id": user.id, "headers": bearer(create_access_token(user.id, organization.id))}


async def post(client: httpx.AsyncClient, headers: dict, project: Project, body: str) -> dict:
    response = await client.post(url(project), headers=headers, json={"body": body})
    assert response.status_code == 201, response.text
    return response.json()


# ── CMT-2 add / CMT-1 list ────────────────────────────────────────────────────


async def test_add_comment(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], web: Project
) -> None:
    response = await client.post(
        url(web), headers=admin["headers"], json={"body": "  Reproduced on Safari 18.  "}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["body"] == "Reproduced on Safari 18."  # stored trimmed
    assert body["author"]["name"] == "Asha Rao"
    assert body["is_edited"] is False
    assert body["created_at"] == body["updated_at"]
    assert set(body) == {"id", "body", "author", "is_edited", "created_at", "updated_at"}
    task = (await client.get(tasks_url(web, "/1"), headers=admin["headers"])).json()
    assert task["comment_count"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"body": "   "},
        {"body": "x" * 10_001},
        {},
        {"body": "Hi", "author_id": 1},  # the author comes from the token only
    ],
)
async def test_invalid_comment_body(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project, payload: dict
) -> None:
    response = await client.post(url(web), headers=admin["headers"], json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_list_oldest_first_with_authors(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    task = await db_session.scalar(select(Task).where(Task.project_id == web.id))
    authors = [await make_user(db_session, organization=acme, name=f"User {n}") for n in range(5)]
    for n, author in enumerate(authors):
        await make_task_comment(db_session, task=task, author=author, body=f"Comment {n}")
    # One test transaction gives every row the same created_at; spread them out.
    await db_session.execute(
        update(TaskComment)
        .where(TaskComment.task_id == task.id)
        .values(created_at=func.now() + TaskComment.id * text("interval '1 second'"))
    )
    other_task = await make_task(db_session, project=web)
    await make_task_comment(db_session, task=other_task, body="Elsewhere")

    with count_queries(engine) as queries:
        response = await client.get(url(web), headers=admin["headers"])

    body = response.json()
    assert [c["body"] for c in body["items"]] == [f"Comment {n}" for n in range(5)]
    assert [c["author"]["name"] for c in body["items"]] == [f"User {n}" for n in range(5)]
    assert body["pagination"]["total_items"] == 5
    # user + project + permissions + task + COUNT + page (authors joined in)
    assert queries.count == 6, str(queries)

    newest = await client.get(url(web, "?order=desc&page_size=2"), headers=admin["headers"])
    assert [c["body"] for c in newest.json()["items"]] == ["Comment 4", "Comment 3"]
    assert (await client.get(url(web, "?order=up"), headers=admin["headers"])).status_code == 422


async def test_unknown_task_is_not_found(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], web: Project
) -> None:
    other = await make_project(db_session)
    await make_task(db_session, project=other)

    assert (await client.get(url(web, number=99), headers=admin["headers"])).status_code == 404
    assert (
        await client.post(url(web, number=99), headers=admin["headers"], json={"body": "Hi"})
    ).status_code == 404
    assert (await client.get(url(web, number=0), headers=admin["headers"])).status_code == 422


# ── CMT-3 edit (author only) ──────────────────────────────────────────────────


async def test_author_edits_own_comment(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    acme: Organization,
    web: Project,
) -> None:
    ravi = await commenter(db_session, acme, web, "Ravi", "view,create,edit")
    comment = await post(client, ravi["headers"], web, "Reproduced on Safari 18.")
    # Let the edit be visibly later than the creation (same transaction in tests).
    await db_session.execute(
        update(TaskComment)
        .where(TaskComment.id == comment["id"])
        .values(created_at=func.now() - text("interval '1 minute'"))
    )
    created = (await client.get(url(web), headers=ravi["headers"])).json()["items"][0]

    edited = await client.patch(
        url(web, f"/{comment['id']}"),
        headers=ravi["headers"],
        json={
            "body": "Reproduced on Safari 18 and iOS.",
            "expected_updated_at": created["updated_at"],
        },
    )

    assert edited.status_code == 200
    body = edited.json()
    assert body["body"] == "Reproduced on Safari 18 and iOS."
    assert body["is_edited"] is True
    assert body["updated_at"] > created["updated_at"]
    assert body["author"] == {"id": ravi["id"], "name": "Ravi"}


async def test_same_text_is_not_an_edit(
    client: httpx.AsyncClient, admin: dict[str, Any], web: Project
) -> None:
    comment = await post(client, admin["headers"], web, "Looks good")

    response = await client.patch(
        url(web, f"/{comment['id']}"),
        headers=admin["headers"],
        json={"body": " Looks good ", "expected_updated_at": comment["updated_at"]},
    )

    assert response.status_code == 200
    assert response.json()["is_edited"] is False
    assert response.json()["updated_at"] == comment["updated_at"]


async def test_only_the_author_can_edit(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    web: Project,
) -> None:
    ravi = await commenter(db_session, acme, web, "Ravi", "view,create,edit")
    author_without_edit = await commenter(db_session, acme, web, "Meera", "view,create")
    ravis = await post(client, ravi["headers"], web, "Ravi's comment")
    meeras = await post(client, author_without_edit["headers"], web, "Meera's comment")

    def edit(comment: dict) -> dict:
        return {"body": "Changed", "expected_updated_at": comment["updated_at"]}

    # The org Admin has every permission, but it isn't their comment.
    by_admin = await client.patch(
        url(web, f"/{ravis['id']}"), headers=admin["headers"], json=edit(ravis)
    )
    # Meera wrote hers, but her role has no comments.edit.
    by_meera = await client.patch(
        url(web, f"/{meeras['id']}"), headers=author_without_edit["headers"], json=edit(meeras)
    )

    assert by_admin.status_code == 403
    assert by_admin.json()["error"]["message"] == "Only the author can edit a comment."
    assert by_meera.status_code == 403
    listed = (await client.get(url(web), headers=admin["headers"])).json()["items"]
    assert [c["body"] for c in listed] == ["Ravi's comment", "Meera's comment"]


async def test_stale_edit(client: httpx.AsyncClient, admin: dict[str, Any], web: Project) -> None:
    comment = await post(client, admin["headers"], web, "First")
    first = await client.patch(
        url(web, f"/{comment['id']}"),
        headers=admin["headers"],
        json={"body": "Second", "expected_updated_at": comment["updated_at"]},
    )

    stale = await client.patch(
        url(web, f"/{comment['id']}"),
        headers=admin["headers"],
        json={"body": "Third", "expected_updated_at": comment["updated_at"]},
    )

    assert first.status_code == 200
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_update"
    assert stale.json()["error"]["details"]["current"]["body"] == "Second"
    missing_version = await client.patch(
        url(web, f"/{comment['id']}"), headers=admin["headers"], json={"body": "x"}
    )
    assert missing_version.status_code == 422


async def test_comments_of_other_tasks_are_not_found(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    web: Project,
) -> None:
    second = await new_task(client, admin["headers"], web)  # WEB-2
    comment = await client.post(
        url(web, number=second["number"]), headers=admin["headers"], json={"body": "On WEB-2"}
    )
    comment_id = comment.json()["id"]
    stamp = {"body": "x", "expected_updated_at": comment.json()["updated_at"]}

    # Reached through WEB-1's URL, WEB-2's comment doesn't exist.
    assert (
        await client.patch(url(web, f"/{comment_id}"), headers=admin["headers"], json=stamp)
    ).status_code == 404
    assert (
        await client.delete(url(web, f"/{comment_id}"), headers=admin["headers"])
    ).status_code == 404
    assert (
        await client.delete(url(web, "/999999999"), headers=admin["headers"])
    ).status_code == 404


# ── CMT-4 delete (author or moderator) ────────────────────────────────────────


async def test_author_deletes_own_comment(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    acme: Organization,
    web: Project,
) -> None:
    ravi = await commenter(db_session, acme, web, "Ravi", "view,create,edit")
    meera = await commenter(db_session, acme, web, "Meera", "view,create,edit")
    ravis = await post(client, ravi["headers"], web, "Ravi's comment")

    not_hers = await client.delete(url(web, f"/{ravis['id']}"), headers=meera["headers"])
    own = await client.delete(url(web, f"/{ravis['id']}"), headers=ravi["headers"])

    assert not_hers.status_code == 403
    assert own.status_code == 204
    assert (
        await client.delete(url(web, f"/{ravis['id']}"), headers=ravi["headers"])
    ).status_code == 404


async def test_moderator_deletes_any_comment(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    acme: Organization,
    web: Project,
) -> None:
    ravi = await commenter(db_session, acme, web, "Ravi", "view,create,edit")
    moderator = await commenter(db_session, acme, web, "Mod", "view,delete")
    ravis = await post(client, ravi["headers"], web, "Off-topic")

    response = await client.delete(url(web, f"/{ravis['id']}"), headers=moderator["headers"])

    assert response.status_code == 204
    # Moderators can delete, but still not edit someone else's words.
    other = await post(client, ravi["headers"], web, "Another")
    edit = await client.patch(
        url(web, f"/{other['id']}"),
        headers=moderator["headers"],
        json={"body": "Censored", "expected_updated_at": other["updated_at"]},
    )
    assert edit.status_code == 403


async def test_author_without_edit_cannot_delete(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    acme: Organization,
    web: Project,
) -> None:
    writer = await commenter(db_session, acme, web, "Writer", "view,create")
    comment = await post(client, writer["headers"], web, "Mine")

    response = await client.delete(url(web, f"/{comment['id']}"), headers=writer["headers"])

    assert response.status_code == 403


# ── Permissions and archived projects ─────────────────────────────────────────


async def test_comment_permissions(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    acme: Organization,
    web: Project,
) -> None:
    reader = await commenter(db_session, acme, web, "Reader", "view")
    tasks_only = await member(db_session, acme, {"tasks": "view"}, web)
    nobody = await outsider(db_session, acme)

    assert (await client.get(url(web), headers=reader["headers"])).status_code == 200
    assert (
        await client.post(url(web), headers=reader["headers"], json={"body": "Hi"})
    ).status_code == 403
    assert (await client.get(url(web), headers=tasks_only)).status_code == 403
    assert (await client.get(url(web), headers=nobody)).status_code == 404


async def test_archived_project_comments_are_read_only(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], web: Project
) -> None:
    comment = await post(client, admin["headers"], web, "Before the archive")
    await db_session.execute(update(Project).where(Project.id == web.id).values(status="archived"))
    headers = admin["headers"]

    writes = [
        await client.post(url(web), headers=headers, json={"body": "After"}),
        await client.patch(
            url(web, f"/{comment['id']}"),
            headers=headers,
            json={"body": "x", "expected_updated_at": comment["updated_at"]},
        ),
        await client.delete(url(web, f"/{comment['id']}"), headers=headers),
    ]

    assert [w.json()["error"]["code"] for w in writes] == ["project_archived"] * 3
    assert len((await client.get(url(web), headers=headers)).json()["items"]) == 1


# ── Real, separate transactions ───────────────────────────────────────────────

RACE_SLUG = "race-comments"


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
            # Comments go with their task (CASCADE); tasks before users.
            await cleanup.execute(delete(Task).where(Task.project_id.in_(project_ids)))
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
    tasks = f"/projects/{project['public_id']}/tasks"
    await real_client.post(tasks, headers=headers, json={"title": "Discuss"})
    return {"headers": headers, "comments": f"{tasks}/1/comments", "task": f"{tasks}/1"}


async def test_two_tabs_editing_the_same_comment(
    real_client: httpx.AsyncClient, engine: AsyncEngine, race: dict[str, Any]
) -> None:
    """Both saves pass the early version check while the row is locked
    elsewhere; the version in the UPDATE's WHERE lets exactly one through."""
    comment = (
        await real_client.post(race["comments"], headers=race["headers"], json={"body": "v1"})
    ).json()
    results: dict[str, int] = {}

    async def save(text_: str) -> None:
        response = await real_client.patch(
            f"{race['comments']}/{comment['id']}",
            headers=race["headers"],
            json={"body": text_, "expected_updated_at": comment["updated_at"]},
        )
        results[text_] = response.status_code

    async with AsyncSession(engine) as holder:
        await holder.execute(
            select(TaskComment.id).where(TaskComment.id == comment["id"]).with_for_update()
        )
        async with anyio.create_task_group() as group:
            group.start_soon(save, "tab A")
            group.start_soon(save, "tab B")
            await anyio.sleep(0.5)
            assert results == {}
            await holder.commit()

    assert sorted(results.values()) == [200, 409]
    winner = next(t for t, code in results.items() if code == 200)
    listed = (await real_client.get(race["comments"], headers=race["headers"])).json()
    assert [c["body"] for c in listed["items"]] == [winner]


async def test_comment_on_a_task_being_deleted(
    real_client: httpx.AsyncClient, engine: AsyncEngine, race: dict[str, Any]
) -> None:
    """The task is deleted (not yet committed) while a comment is being added.
    The comment's foreign key check waits, then fails: 404, not a 500."""
    responses: list[httpx.Response] = []

    async def add() -> None:
        responses.append(
            await real_client.post(
                race["comments"], headers=race["headers"], json={"body": "Too late"}
            )
        )

    async with AsyncSession(engine) as deleter:
        await deleter.execute(delete(Task).where(Task.title == "Discuss"))
        async with anyio.create_task_group() as group:
            group.start_soon(add)
            await anyio.sleep(0.5)
            assert responses == []  # waiting for the delete's row lock
            await deleter.commit()

    assert responses[0].status_code == 404
    assert responses[0].json()["error"]["message"] == "Task not found."
