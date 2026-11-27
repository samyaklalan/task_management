"""M5b: USR-5 PATCH /users/{id}, USR-6 PUT and USR-7 DELETE /users/{id}/org-role."""

from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.permissions import Feature
from app.core.security import create_access_token, hash_password
from app.db.session import get_session
from app.main import create_app
from app.models import Organization, RefreshToken, Role, RolePermission, User, UserRole
from tests.factories import make_organization, make_project, make_role, make_user

REGISTER = {
    "organization_name": "Acme Corp",
    "organization_slug": "acme",
    "name": "Asha Rao",
    "email": "asha@acme.com",
    "password": "correct-horse-battery",
}
RAVI_PASSWORD = "ravis-own-password"


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


@pytest.fixture
async def ravi(db_session: AsyncSession, acme: Organization) -> User:
    """A colleague who can log in, with no role."""
    return await make_user(
        db_session,
        organization=acme,
        name="Ravi Kumar",
        email="ravi@acme.com",
        password_hash=await hash_password(RAVI_PASSWORD),
    )


async def role_with(
    session: AsyncSession, organization: Organization, name: str, grants: dict[Feature, str]
) -> Role:
    """``grants``: {Feature.USERS: "view,edit"}."""
    role = await make_role(session, organization=organization, name=name)
    for feature, actions in grants.items():
        flags = {f"{action}_perm": True for action in actions.split(",")}
        session.add(RolePermission(role_id=role.id, feature=feature.value, **flags))
    await session.flush()
    return role


async def give_org_role(session: AsyncSession, user: User, role: Role) -> dict[str, str]:
    session.add(UserRole(user_id=user.id, role_id=role.id))
    await session.flush()
    return bearer(create_access_token(user.id, user.organization_id))


async def user_json(client: httpx.AsyncClient, admin: dict[str, Any], user_id: int) -> dict:
    items = (await client.get("/users?page_size=100", headers=admin["headers"])).json()["items"]
    return next(item for item in items if item["id"] == user_id)


async def org_role_rows(session: AsyncSession, user_id: int) -> list[int]:
    rows = await session.scalars(
        select(UserRole.role_id).where(UserRole.user_id == user_id, UserRole.project_id.is_(None))
    )
    return list(rows)


# ── USR-5 PATCH /users/{id} ───────────────────────────────────────────────────


async def test_rename_a_user(client: httpx.AsyncClient, admin: dict[str, Any], ravi: User) -> None:
    seen = await user_json(client, admin, ravi.id)

    response = await client.patch(
        f"/users/{ravi.id}",
        headers=admin["headers"],
        json={"name": "Ravi K.", "expected_updated_at": seen["updated_at"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["name"], body["email"], body["is_active"]) == ("Ravi K.", "ravi@acme.com", True)
    assert body["updated_at"] > seen["updated_at"]


async def test_deactivate_logs_the_user_out_everywhere_at_once(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], ravi: User
) -> None:
    logins = [
        await client.post("/auth/login", json={"email": "ravi@acme.com", "password": RAVI_PASSWORD})
        for _ in range(2)  # laptop and phone
    ]
    ravi_access = bearer(logins[0].json()["access_token"])
    seen = await user_json(client, admin, ravi.id)

    response = await client.patch(
        f"/users/{ravi.id}",
        headers=admin["headers"],
        json={"is_active": False, "expected_updated_at": seen["updated_at"]},
    )

    assert response.status_code == 200 and response.json()["is_active"] is False
    active = await db_session.scalar(
        select(func.count())
        .select_from(RefreshToken)
        .where(RefreshToken.user_id == ravi.id, RefreshToken.revoked_at.is_(None))
    )
    assert active == 0
    # His access token is still unexpired, but it no longer works.
    assert (await client.get("/users/me", headers=ravi_access)).status_code == 401
    refresh = await client.post(
        "/auth/refresh", json={"refresh_token": logins[1].json()["refresh_token"]}
    )
    assert refresh.status_code == 401


async def test_reactivated_user_can_log_in_again(
    client: httpx.AsyncClient, admin: dict[str, Any], ravi: User
) -> None:
    seen = await user_json(client, admin, ravi.id)
    off = await client.patch(
        f"/users/{ravi.id}",
        headers=admin["headers"],
        json={"is_active": False, "expected_updated_at": seen["updated_at"]},
    )
    on = await client.patch(
        f"/users/{ravi.id}",
        headers=admin["headers"],
        json={"is_active": True, "expected_updated_at": off.json()["updated_at"]},
    )

    login = await client.post(
        "/auth/login", json={"email": "ravi@acme.com", "password": RAVI_PASSWORD}
    )
    assert on.status_code == 200 and on.json()["is_active"] is True
    assert login.status_code == 200


async def test_you_cannot_deactivate_yourself(
    client: httpx.AsyncClient, admin: dict[str, Any]
) -> None:
    me = await user_json(client, admin, admin["id"])

    deactivate = await client.patch(
        f"/users/{admin['id']}",
        headers=admin["headers"],
        json={"is_active": False, "expected_updated_at": me["updated_at"]},
    )
    rename = await client.patch(
        f"/users/{admin['id']}",
        headers=admin["headers"],
        json={"name": "Asha R.", "expected_updated_at": me["updated_at"]},
    )

    assert deactivate.status_code == 409
    assert deactivate.json()["error"]["code"] == "self_lockout"
    assert rename.status_code == 200  # renaming yourself is fine


async def test_stale_update_returns_the_current_user(
    client: httpx.AsyncClient, admin: dict[str, Any], ravi: User
) -> None:
    seen = await user_json(client, admin, ravi.id)
    first = await client.patch(
        f"/users/{ravi.id}",
        headers=admin["headers"],
        json={"name": "First", "expected_updated_at": seen["updated_at"]},
    )

    second = await client.patch(
        f"/users/{ravi.id}",
        headers=admin["headers"],
        json={"is_active": False, "expected_updated_at": seen["updated_at"]},
    )

    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "stale_update"
    assert error["details"]["current"] == first.json()
    assert (await user_json(client, admin, ravi.id))["is_active"] is True  # not applied


@pytest.mark.parametrize("which", ["other-organization", "unknown"])
async def test_users_outside_my_organization_are_not_found(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any], which: str
) -> None:
    if which == "other-organization":
        outsider = await make_user(db_session, organization=await make_organization(db_session))
        user_id = outsider.id
    else:
        user_id = 10**12

    patch = await client.patch(
        f"/users/{user_id}",
        headers=admin["headers"],
        json={"is_active": False, "expected_updated_at": "2026-01-01T00:00:00Z"},
    )
    put = await client.put(
        f"/users/{user_id}/org-role", headers=admin["headers"], json={"role_id": 1}
    )
    remove = await client.delete(f"/users/{user_id}/org-role", headers=admin["headers"])

    assert [patch.status_code, put.status_code, remove.status_code] == [404, 404, 404]


@pytest.mark.parametrize("user_id", ["abc", "0", "-1", str(2**63)])
async def test_invalid_user_id_in_the_url(
    client: httpx.AsyncClient, admin: dict[str, Any], user_id: str
) -> None:
    response = await client.delete(f"/users/{user_id}/org-role", headers=admin["headers"])

    assert response.status_code == 422  # not a database error
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "body",
    [
        {"is_active": None, "expected_updated_at": "2026-01-01T00:00:00Z"},
        {"is_active": False},
        {"email": "new@acme.com", "expected_updated_at": "2026-01-01T00:00:00Z"},
    ],
    ids=["null-is_active", "no-expected_updated_at", "email-not-editable"],
)
async def test_invalid_update_body(
    client: httpx.AsyncClient, admin: dict[str, Any], ravi: User, body: dict
) -> None:
    response = await client.patch(f"/users/{ravi.id}", headers=admin["headers"], json=body)

    assert response.status_code == 422


async def test_update_needs_users_edit(
    client: httpx.AsyncClient, db_session: AsyncSession, acme: Organization, ravi: User
) -> None:
    viewer = await make_user(db_session, organization=acme)
    headers = await give_org_role(
        db_session, viewer, await role_with(db_session, acme, "Viewer", {Feature.USERS: "view"})
    )

    response = await client.patch(
        f"/users/{ravi.id}",
        headers=headers,
        json={"is_active": False, "expected_updated_at": "2026-01-01T00:00:00Z"},
    )

    assert response.status_code == 403


# ── USR-6 PUT /users/{id}/org-role ────────────────────────────────────────────


async def test_give_and_replace_an_org_role(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    ravi: User,
) -> None:
    hr = await role_with(db_session, acme, "HR", {Feature.USERS: "view"})
    manager = await role_with(db_session, acme, "Manager", {Feature.PROJECTS: "view,create"})
    ravi_headers = bearer(create_access_token(ravi.id, acme.id))
    assert (await client.get("/users", headers=ravi_headers)).status_code == 403

    given = await client.put(
        f"/users/{ravi.id}/org-role", headers=admin["headers"], json={"role_id": hr.id}
    )

    assert given.status_code == 200
    assert given.json() == {"user_id": ravi.id, "role": {"id": hr.id, "name": "HR"}}
    assert (await client.get("/users", headers=ravi_headers)).status_code == 200  # takes effect
    assert (await user_json(client, admin, ravi.id))["org_role"] == {"id": hr.id, "name": "HR"}

    replaced = await client.put(
        f"/users/{ravi.id}/org-role", headers=admin["headers"], json={"role_id": manager.id}
    )

    assert replaced.json()["role"]["name"] == "Manager"
    assert await org_role_rows(db_session, ravi.id) == [manager.id]  # one row, replaced
    assert (await client.get("/users", headers=ravi_headers)).status_code == 403  # HR gone


@pytest.mark.parametrize("which", ["other-organization", "unknown"])
async def test_role_must_belong_to_my_organization(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    ravi: User,
    which: str,
) -> None:
    if which == "other-organization":
        role_id = (await make_role(db_session, organization=await make_organization(db_session))).id
    else:
        role_id = 10**12

    response = await client.put(
        f"/users/{ravi.id}/org-role", headers=admin["headers"], json={"role_id": role_id}
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_reference"
    assert error["details"][0]["field"] == "role_id"
    assert await org_role_rows(db_session, ravi.id) == []


async def test_you_cannot_change_your_own_org_role(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
) -> None:
    weaker = await role_with(db_session, acme, "Member", {})

    response = await client.put(
        f"/users/{admin['id']}/org-role", headers=admin["headers"], json={"role_id": weaker.id}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "self_lockout"
    me = (await client.get("/users/me", headers=admin["headers"])).json()
    assert me["org_role"]["name"] == "Admin"  # unchanged


async def test_org_roles_need_roles_edit_not_users_edit(
    client: httpx.AsyncClient, db_session: AsyncSession, acme: Organization, ravi: User
) -> None:
    hr_admin = await make_user(db_session, organization=acme)
    role = await role_with(db_session, acme, "HR admin", {Feature.USERS: "view,create,edit,delete"})
    headers = await give_org_role(db_session, hr_admin, role)

    put = await client.put(f"/users/{ravi.id}/org-role", headers=headers, json={"role_id": role.id})
    remove = await client.delete(f"/users/{ravi.id}/org-role", headers=headers)

    assert [put.status_code, remove.status_code] == [403, 403]


# ── USR-7 DELETE /users/{id}/org-role ─────────────────────────────────────────


async def test_remove_org_role_keeps_project_roles(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin: dict[str, Any],
    acme: Organization,
    ravi: User,
) -> None:
    hr = await role_with(db_session, acme, "HR", {Feature.USERS: "view"})
    await give_org_role(db_session, ravi, hr)
    project = await make_project(db_session, organization=acme)
    db_session.add(UserRole(user_id=ravi.id, role_id=hr.id, project_id=project.id))
    await db_session.flush()

    response = await client.delete(f"/users/{ravi.id}/org-role", headers=admin["headers"])

    assert response.status_code == 204
    assert await org_role_rows(db_session, ravi.id) == []
    project_roles = await db_session.scalar(
        select(func.count()).select_from(UserRole).where(UserRole.project_id == project.id)
    )
    assert project_roles == 1
    again = await client.delete(f"/users/{ravi.id}/org-role", headers=admin["headers"])
    assert again.status_code == 204  # nothing left to remove: still fine


async def test_you_cannot_remove_your_own_org_role(
    client: httpx.AsyncClient, admin: dict[str, Any]
) -> None:
    response = await client.delete(f"/users/{admin['id']}/org-role", headers=admin["headers"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "self_lockout"


# ── Two admins assigning roles at the same moment (real transactions) ─────────


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
            org_ids = select(Organization.id).where(Organization.slug == "race-roles")
            await cleanup.execute(delete(User).where(User.organization_id.in_(org_ids)))
            await cleanup.execute(delete(Role).where(Role.organization_id.in_(org_ids)))
            await cleanup.execute(delete(Organization).where(Organization.slug == "race-roles"))
            await cleanup.commit()


async def test_simultaneous_role_changes_leave_exactly_one_org_role(
    real_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    signed_up = await real_client.post(
        "/auth/register", json=REGISTER | {"organization_slug": "race-roles"}
    )
    headers = bearer(signed_up.json()["access_token"])
    async with AsyncSession(engine, expire_on_commit=False) as session:
        organization = await session.scalar(
            select(Organization).where(Organization.slug == "race-roles")
        )
        ravi = await make_user(session, organization=organization)
        roles = [await make_role(session, organization=organization, name=n) for n in ("A", "B")]
        await session.commit()

    statuses: list[int] = []

    async def assign(role_id: int) -> None:
        response = await real_client.put(
            f"/users/{ravi.id}/org-role", headers=headers, json={"role_id": role_id}
        )
        statuses.append(response.status_code)

    async with anyio.create_task_group() as group:
        for role in roles:
            group.start_soon(assign, role.id)

    async with AsyncSession(engine) as session:
        rows = await org_role_rows(session, ravi.id)
    assert statuses == [200, 200]  # both succeed: the last one wins
    assert len(rows) == 1 and rows[0] in {role.id for role in roles}
