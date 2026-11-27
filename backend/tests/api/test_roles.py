"""M6: ROLE-1 to ROLE-4, roles and their permissions, end to end."""

from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.permissions import Feature
from app.core.security import create_access_token
from app.db.session import get_session
from app.main import create_app
from app.models import Organization, Role, RolePermission, User, UserRole
from tests.factories import make_organization, make_project, make_role, make_user
from tests.utils.query_counter import count_queries

REGISTER = {
    "organization_name": "Acme Corp",
    "organization_slug": "acme",
    "name": "Asha Rao",
    "email": "asha@acme.com",
    "password": "correct-horse-battery",
}
ALL_FEATURES = [feature.value for feature in Feature]
OFF = {"view": False, "create": False, "edit": False, "delete": False}
ON = {"view": True, "create": True, "edit": True, "delete": True}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post("/auth/register", json=REGISTER)
    assert response.status_code == 201
    body = response.json()
    return {"id": body["user"]["id"], "headers": bearer(body["access_token"])}


@pytest.fixture
async def acme(db_session: AsyncSession, admin: dict[str, Any]) -> Organization:
    return await db_session.scalar(select(Organization).where(Organization.slug == "acme"))


def by_feature(role: dict) -> dict[str, dict[str, bool]]:
    return {
        item["feature"]: {k: v for k, v in item.items() if k != "feature"}
        for item in role["permissions"]
    }


async def create(client: httpx.AsyncClient, admin: dict, **body: Any) -> dict:
    response = await client.post("/roles", headers=admin["headers"], json=body)
    assert response.status_code == 201, response.text
    return response.json()


# ── ROLE-1 GET /roles ─────────────────────────────────────────────────────────


async def test_list_shows_the_admin_role_with_all_ten_features(
    client: httpx.AsyncClient, admin: dict[str, Any]
) -> None:
    response = await client.get("/roles", headers=admin["headers"])

    assert response.status_code == 200
    [role] = response.json()["items"]
    assert set(role) == {"id", "name", "assigned_count", "permissions", "created_at", "updated_at"}
    assert (role["name"], role["assigned_count"]) == ("Admin", 1)
    assert [item["feature"] for item in role["permissions"]] == ALL_FEATURES
    assert all(flags == ON for flags in by_feature(role).values())


async def test_list_counts_holders_and_fills_missing_features(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], acme: Organization
) -> None:
    # A role stored with only one permission row (e.g. made before a feature existed).
    sparse = await make_role(db_session, organization=acme, name="developer")
    db_session.add(RolePermission(role_id=sparse.id, feature="tasks", view_perm=True))
    project = await make_project(db_session, organization=acme)
    for _ in range(2):
        user = await make_user(db_session, organization=acme)
        db_session.add(UserRole(user_id=user.id, role_id=sparse.id, project_id=project.id))
    await make_role(db_session, organization=await make_organization(db_session), name="Outsider")
    await db_session.flush()

    items = (await client.get("/roles", headers=admin["headers"])).json()["items"]

    assert [item["name"] for item in items] == ["Admin", "developer"]  # case-insensitive order
    developer = items[1]
    assert developer["assigned_count"] == 2  # project roles count too
    flags = by_feature(developer)
    assert list(flags) == ALL_FEATURES
    assert flags["tasks"] == OFF | {"view": True}
    assert flags["comments"] == OFF


async def test_list_query_count_does_not_grow_with_roles(
    client: httpx.AsyncClient, engine: AsyncEngine, admin: dict[str, Any]
) -> None:
    for number in range(5):
        await create(client, admin, name=f"Role {number}")

    with count_queries(engine) as queries:
        await client.get("/roles", headers=admin["headers"])

    # user + permissions check + roles with counts + all their permission rows
    assert queries.count == 4, str(queries)


async def test_list_needs_roles_view(
    client: httpx.AsyncClient, db_session: AsyncSession, acme: Organization
) -> None:
    nobody = await make_user(db_session, organization=acme)
    headers = bearer(create_access_token(nobody.id, acme.id))

    assert (await client.get("/roles", headers=headers)).status_code == 403


# ── ROLE-2 POST /roles ────────────────────────────────────────────────────────


async def test_create_a_role(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any]
) -> None:
    role = await create(
        client,
        admin,
        name="  Developer ",
        permissions=[
            {"feature": "projects", "view": True},
            {"feature": "tasks", "view": True, "create": True, "edit": True},
        ],
    )

    assert (role["name"], role["assigned_count"]) == ("Developer", 0)
    flags = by_feature(role)
    assert flags["projects"] == OFF | {"view": True}
    assert flags["tasks"] == {"view": True, "create": True, "edit": True, "delete": False}
    assert flags["users"] == OFF
    stored = await db_session.scalar(
        select(func.count()).select_from(RolePermission).where(RolePermission.role_id == role["id"])
    )
    assert stored == 10  # a row for every feature
    assert role in (await client.get("/roles", headers=admin["headers"])).json()["items"]


async def test_a_role_without_permissions(
    client: httpx.AsyncClient, admin: dict[str, Any]
) -> None:
    role = await create(client, admin, name="Guest")

    assert all(flags == OFF for flags in by_feature(role).values())


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"name": "X", "permissions": [{"feature": "billing", "view": True}]}, "feature"),
        (
            {
                "name": "X",
                "permissions": [{"feature": "tasks"}, {"feature": "tasks", "edit": True}],
            },
            "only once",
        ),
        ({"name": "X", "permissions": [{"feature": "tasks", "approve": True}]}, "approve"),
        ({"name": "   "}, "name"),
        ({"name": "x" * 51}, "name"),
        ({"permissions": []}, "name"),
    ],
    ids=["unknown-feature", "duplicate-feature", "unknown-action", "blank", "too-long", "no-name"],
)
async def test_invalid_role_body(
    client: httpx.AsyncClient, admin: dict[str, Any], body: dict, fragment: str
) -> None:
    response = await client.post("/roles", headers=admin["headers"], json=body)

    assert response.status_code == 422
    assert fragment in response.text


async def test_create_needs_roles_create(
    client: httpx.AsyncClient, db_session: AsyncSession, acme: Organization
) -> None:
    viewer = await make_user(db_session, organization=acme)
    role = await make_role(db_session, organization=acme, name="Viewer")
    db_session.add(RolePermission(role_id=role.id, feature="roles", view_perm=True))
    db_session.add(UserRole(user_id=viewer.id, role_id=role.id))
    await db_session.flush()
    headers = bearer(create_access_token(viewer.id, acme.id))

    assert (await client.get("/roles", headers=headers)).status_code == 200
    assert (await client.post("/roles", headers=headers, json={"name": "X"})).status_code == 403


# ── ROLE-3 PATCH /roles/{id} ──────────────────────────────────────────────────


async def test_permissions_are_replaced_completely(
    client: httpx.AsyncClient, admin: dict[str, Any]
) -> None:
    role = await create(
        client,
        admin,
        name="Developer",
        permissions=[{"feature": "tasks", **ON}, {"feature": "comments", "view": True}],
    )

    response = await client.patch(
        f"/roles/{role['id']}",
        headers=admin["headers"],
        json={
            "permissions": [{"feature": "projects", "view": True}],
            "expected_updated_at": role["updated_at"],
        },
    )

    assert response.status_code == 200
    updated = response.json()
    flags = by_feature(updated)
    assert flags["projects"] == OFF | {"view": True}
    assert flags["tasks"] == OFF  # not listed any more: switched off
    assert flags["comments"] == OFF
    assert updated["name"] == "Developer"  # not sent: unchanged
    assert updated["updated_at"] > role["updated_at"]


async def test_rename_keeps_the_permissions(
    client: httpx.AsyncClient, admin: dict[str, Any]
) -> None:
    role = await create(client, admin, name="Dev", permissions=[{"feature": "tasks", **ON}])

    response = await client.patch(
        f"/roles/{role['id']}",
        headers=admin["headers"],
        json={"name": "Developer", "expected_updated_at": role["updated_at"]},
    )

    assert response.json()["name"] == "Developer"
    assert by_feature(response.json())["tasks"] == ON


async def test_permission_change_takes_effect_on_the_next_request(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], acme: Organization
) -> None:
    role = await create(client, admin, name="HR", permissions=[{"feature": "users", "view": True}])
    ravi = await make_user(db_session, organization=acme)
    db_session.add(UserRole(user_id=ravi.id, role_id=role["id"]))
    await db_session.flush()
    ravi_headers = bearer(create_access_token(ravi.id, acme.id))
    assert (await client.get("/users", headers=ravi_headers)).status_code == 200

    await client.patch(
        f"/roles/{role['id']}",
        headers=admin["headers"],
        json={"permissions": [], "expected_updated_at": role["updated_at"]},
    )

    assert (await client.get("/users", headers=ravi_headers)).status_code == 403


async def test_stale_role_edit_is_refused(
    client: httpx.AsyncClient, admin: dict[str, Any]
) -> None:
    role = await create(client, admin, name="Dev")
    first = await client.patch(
        f"/roles/{role['id']}",
        headers=admin["headers"],
        json={
            "permissions": [{"feature": "tasks", **ON}],
            "expected_updated_at": role["updated_at"],
        },
    )

    second = await client.patch(
        f"/roles/{role['id']}",
        headers=admin["headers"],
        json={"name": "Overwritten", "expected_updated_at": role["updated_at"]},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "stale_update"
    assert second.json()["error"]["details"]["current"] == first.json()


async def test_you_cannot_remove_roles_edit_from_your_own_role(
    client: httpx.AsyncClient, admin: dict[str, Any]
) -> None:
    [admin_role] = (await client.get("/roles", headers=admin["headers"])).json()["items"]
    everything_but_roles_edit = [{"feature": f, **ON} for f in ALL_FEATURES if f != "roles"] + [
        {"feature": "roles", **ON, "edit": False}
    ]

    for permissions in (everything_but_roles_edit, []):
        response = await client.patch(
            f"/roles/{admin_role['id']}",
            headers=admin["headers"],
            json={"permissions": permissions, "expected_updated_at": admin_role["updated_at"]},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "self_lockout"

    # Other changes to your own role are fine, as long as roles.edit stays.
    keep = [{"feature": "roles", **ON}, {"feature": "users", "view": True}]
    ok = await client.patch(
        f"/roles/{admin_role['id']}",
        headers=admin["headers"],
        json={
            "name": "Owner",
            "permissions": keep,
            "expected_updated_at": admin_role["updated_at"],
        },
    )
    assert ok.status_code == 200 and ok.json()["name"] == "Owner"


async def test_unknown_or_foreign_role_is_not_found(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any]
) -> None:
    foreign = await make_role(db_session, organization=await make_organization(db_session))

    for role_id in (foreign.id, 10**12):
        patch = await client.patch(
            f"/roles/{role_id}",
            headers=admin["headers"],
            json={"name": "X", "expected_updated_at": "2026-01-01T00:00:00Z"},
        )
        remove = await client.delete(f"/roles/{role_id}", headers=admin["headers"])
        assert (patch.status_code, remove.status_code) == (404, 404)


@pytest.mark.parametrize(
    "body",
    [
        {"name": None, "expected_updated_at": "2026-01-01T00:00:00Z"},
        {"permissions": None, "expected_updated_at": "2026-01-01T00:00:00Z"},
        {"name": "X"},
    ],
    ids=["null-name", "null-permissions", "no-expected_updated_at"],
)
async def test_invalid_update_body(
    client: httpx.AsyncClient, admin: dict[str, Any], body: dict
) -> None:
    role = await create(client, admin, name="Dev")

    response = await client.patch(f"/roles/{role['id']}", headers=admin["headers"], json=body)

    assert response.status_code == 422


# ── ROLE-4 DELETE /roles/{id} ─────────────────────────────────────────────────


async def test_delete_an_unused_role(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any]
) -> None:
    role = await create(client, admin, name="Temp", permissions=[{"feature": "tasks", **ON}])

    response = await client.delete(f"/roles/{role['id']}", headers=admin["headers"])

    assert response.status_code == 204
    assert await db_session.get(Role, role["id"]) is None
    rows = await db_session.scalar(
        select(func.count()).select_from(RolePermission).where(RolePermission.role_id == role["id"])
    )
    assert rows == 0  # permission rows went with it


async def test_a_role_in_use_cannot_be_deleted(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], acme: Organization
) -> None:
    role = await create(client, admin, name="Developer")
    project = await make_project(db_session, organization=acme)
    for _ in range(3):
        user = await make_user(db_session, organization=acme)
        db_session.add(UserRole(user_id=user.id, role_id=role["id"], project_id=project.id))
    await db_session.flush()

    response = await client.delete(f"/roles/{role['id']}", headers=admin["headers"])

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "role_in_use"
    assert error["details"] == {"assigned_count": 3}
    assert await db_session.get(Role, role["id"]) is not None


async def test_delete_needs_roles_delete(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], acme: Organization
) -> None:
    role = await create(client, admin, name="Temp")
    editor = await make_user(db_session, organization=acme)
    editor_role = await make_role(db_session, organization=acme, name="Role editor")
    db_session.add(
        RolePermission(role_id=editor_role.id, feature="roles", view_perm=True, edit_perm=True)
    )
    db_session.add(UserRole(user_id=editor.id, role_id=editor_role.id))
    await db_session.flush()

    response = await client.delete(
        f"/roles/{role['id']}", headers=bearer(create_access_token(editor.id, acme.id))
    )

    assert response.status_code == 403


# ── Simultaneous edits of one role (real transactions) ────────────────────────


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
            org_ids = select(Organization.id).where(Organization.slug == "race-role-edit")
            await cleanup.execute(delete(User).where(User.organization_id.in_(org_ids)))
            await cleanup.execute(delete(Role).where(Role.organization_id.in_(org_ids)))
            await cleanup.execute(delete(Organization).where(Organization.slug == "race-role-edit"))
            await cleanup.commit()


async def test_two_simultaneous_permission_edits_one_wins(
    real_client: httpx.AsyncClient,
) -> None:
    signed_up = await real_client.post(
        "/auth/register", json=REGISTER | {"organization_slug": "race-role-edit"}
    )
    admin = {"headers": bearer(signed_up.json()["access_token"])}
    role = await create(real_client, admin, name="Dev")
    statuses: list[int] = []

    async def edit(feature: str) -> None:
        response = await real_client.patch(
            f"/roles/{role['id']}",
            headers=admin["headers"],
            json={
                "permissions": [{"feature": feature, **ON}],
                "expected_updated_at": role["updated_at"],
            },
        )
        statuses.append(response.status_code)

    async with anyio.create_task_group() as group:
        group.start_soon(edit, "tasks")
        group.start_soon(edit, "comments")

    assert sorted(statuses) == [200, 409]
    flags = by_feature(
        next(
            r
            for r in (await real_client.get("/roles", headers=admin["headers"])).json()["items"]
            if r["id"] == role["id"]
        )
    )
    # Exactly one edit was applied, never a mix of both.
    assert [flags["tasks"], flags["comments"]] in ([ON, OFF], [OFF, ON])
