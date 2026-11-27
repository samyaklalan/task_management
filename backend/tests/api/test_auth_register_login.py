"""M1: AUTH-1 POST /auth/register and AUTH-2 POST /auth/login, end to end."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import httpx
import pytest
from argon2 import PasswordHasher
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.security import decode_access_token, hash_password, hash_token
from app.db.session import get_session
from app.main import create_app
from app.models import Organization, RefreshToken, Role, RolePermission, User, UserRole
from tests.factories import make_user

REGISTER = {
    "organization_name": "Acme Corp",
    "name": "Asha Rao",
    "email": "asha@acme.com",
    "password": "correct-horse-battery",
}


async def count(session: AsyncSession, model: type, *where: Any) -> int:
    return await session.scalar(select(func.count()).select_from(model).where(*where))


# ── AUTH-1 register ───────────────────────────────────────────────────────────


async def test_register_creates_everything_and_logs_in(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    response = await client.post("/auth/register", json=REGISTER)

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {
        "access_token",
        "token_type",
        "expires_in",
        "refresh_token",
        "refresh_expires_in",
        "user",
    }
    assert (body["token_type"], body["expires_in"], body["refresh_expires_in"]) == (
        "bearer",
        900,
        14 * 24 * 3600,
    )
    assert body["user"]["name"] == "Asha Rao" and body["user"]["email"] == "asha@acme.com"

    # The organization, with a slug made from its name.
    organization = await db_session.scalar(select(Organization))
    assert (organization.name, organization.slug) == ("Acme Corp", "acme-corp")

    # The Admin role with all 40 permissions.
    role = await db_session.scalar(select(Role).where(Role.organization_id == organization.id))
    assert role.name == "Admin"
    permissions = (
        await db_session.scalars(select(RolePermission).where(RolePermission.role_id == role.id))
    ).all()
    assert len(permissions) == 10
    assert all(p.view_perm and p.create_perm and p.edit_perm and p.delete_perm for p in permissions)

    # The user, with an Argon2 hash (never the password itself), and the org-wide role.
    user_id = body["user"]["id"]
    password_hash = await db_session.scalar(select(User.password_hash).where(User.id == user_id))
    assert password_hash.startswith("$argon2id$") and "correct-horse" not in password_hash
    assignment = await db_session.scalar(select(UserRole).where(UserRole.user_id == user_id))
    assert (assignment.role_id, assignment.project_id) == (role.id, None)

    # The access token names this user; only the refresh token's hash is stored.
    claims = decode_access_token(body["access_token"])
    assert (claims.user_id, claims.organization_id) == (user_id, organization.id)
    stored = await db_session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(body["refresh_token"]))
    )
    assert stored.user_id == user_id and stored.revoked_at is None
    assert stored.expires_at - datetime.now(UTC) > timedelta(days=13, hours=23)


async def test_register_with_an_explicit_slug(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    response = await client.post("/auth/register", json=REGISTER | {"organization_slug": "acme"})

    assert response.status_code == 201
    assert await db_session.scalar(select(Organization.slug)) == "acme"


async def test_email_is_saved_lowercase(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    response = await client.post("/auth/register", json=REGISTER | {"email": "Asha@ACME.com"})

    assert response.json()["user"]["email"] == "asha@acme.com"


async def test_taken_slug_is_a_conflict_and_nothing_is_created(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    first = await client.post("/auth/register", json=REGISTER)
    assert first.status_code == 201

    second = await client.post(
        "/auth/register", json=REGISTER | {"email": "ravi@acme.com", "name": "Ravi"}
    )

    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "conflict"
    assert error["details"] == [
        {"field": "organization_slug", "message": "This organization URL is already taken."}
    ]
    assert await count(db_session, Organization) == 1
    assert await count(db_session, User) == 1


async def test_taken_email_is_a_conflict_and_the_new_organization_is_rolled_back(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await client.post("/auth/register", json=REGISTER)

    second = await client.post(
        "/auth/register",
        json=REGISTER | {"organization_name": "Other Org", "email": "ASHA@acme.com"},
    )

    assert second.status_code == 409
    assert second.json()["error"]["details"][0]["field"] == "email"
    # The "Other Org" organization and its role were inserted before the email
    # failed; the whole sign-up was rolled back.
    assert await count(db_session, Organization) == 1
    assert await count(db_session, Role) == 1


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"password": "short"}, "password"),
        ({"email": "not-an-email"}, "email"),
        ({"organization_slug": "Bad Slug"}, "organization_slug"),
        ({"is_admin": True}, "is_admin"),
    ],
)
async def test_invalid_register_body_is_a_validation_error(
    client: httpx.AsyncClient, db_session: AsyncSession, changes: dict[str, Any], field: str
) -> None:
    response = await client.post("/auth/register", json=REGISTER | changes)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert [detail["field"] for detail in error["details"]] == [field]
    assert await count(db_session, Organization) == 0


# ── AUTH-2 login ──────────────────────────────────────────────────────────────


@pytest.fixture
async def registered(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post("/auth/register", json=REGISTER)
    assert response.status_code == 201
    return response.json()


async def test_login(
    client: httpx.AsyncClient, db_session: AsyncSession, registered: dict[str, Any]
) -> None:
    response = await client.post(
        "/auth/login", json={"email": "Asha@Acme.com", "password": "correct-horse-battery"}
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["user"] == registered["user"]
    assert decode_access_token(body["access_token"]).user_id == registered["user"]["id"]
    # Each login is a new session: a second, different refresh token.
    assert body["refresh_token"] != registered["refresh_token"]
    sessions = RefreshToken.user_id == registered["user"]["id"]
    assert await count(db_session, RefreshToken, sessions) == 2


@pytest.mark.parametrize(
    "credentials",
    [
        {"email": "asha@acme.com", "password": "wrong-password"},
        {"email": "nobody@acme.com", "password": "correct-horse-battery"},
    ],
    ids=["wrong-password", "unknown-email"],
)
async def test_failed_login_gives_one_answer(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    registered: dict[str, Any],
    credentials: dict[str, str],
) -> None:
    response = await client.post("/auth/login", json=credentials)

    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": "invalid_credentials",
        "message": "Invalid email or password.",
    }
    assert await count(db_session, RefreshToken) == 1  # only the sign-up session


async def test_inactive_user_gets_the_same_answer(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(
        db_session,
        email="gone@acme.com",
        password_hash=await hash_password("correct-horse-battery"),
        is_active=False,
    )

    response = await client.post(
        "/auth/login", json={"email": "gone@acme.com", "password": "correct-horse-battery"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_login_upgrades_an_old_password_hash(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash("old-password")
    user = await make_user(db_session, email="old@acme.com", password_hash=weak)

    response = await client.post(
        "/auth/login", json={"email": "old@acme.com", "password": "old-password"}
    )

    assert response.status_code == 200
    new_hash = await db_session.scalar(select(User.password_hash).where(User.id == user.id))
    assert new_hash != weak and new_hash.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    # And the old password still works with the new hash.
    again = await client.post(
        "/auth/login", json={"email": "old@acme.com", "password": "old-password"}
    )
    assert again.status_code == 200


async def test_login_body_is_validated(client: httpx.AsyncClient) -> None:
    response = await client.post("/auth/login", json={"email": "asha@acme.com"})

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "password"


# ── Two sign-ups at the same moment (real, separate transactions) ─────────────


async def test_simultaneous_sign_ups_with_one_email_create_one_account(
    engine: AsyncEngine,
) -> None:
    """Both requests pass validation; the unique index lets exactly one win.
    The other gets 409, not a 500 (insert and catch, not check-then-insert)."""
    application = create_app()

    async def separate_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    application.dependency_overrides[get_session] = separate_session
    transport = httpx.ASGITransport(app=application)
    statuses: list[int] = []
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1") as http:

            async def sign_up(slug: str) -> None:
                body = REGISTER | {"organization_slug": slug, "email": "race@acme.com"}
                statuses.append((await http.post("/auth/register", json=body)).status_code)

            async with anyio.create_task_group() as group:
                group.start_soon(sign_up, "race-one")
                group.start_soon(sign_up, "race-two")
    finally:
        async with AsyncSession(engine) as cleanup:  # these rows were really committed
            org_ids = select(Organization.id).where(Organization.slug.in_(["race-one", "race-two"]))
            await cleanup.execute(delete(User).where(User.organization_id.in_(org_ids)))
            await cleanup.execute(delete(Role).where(Role.organization_id.in_(org_ids)))
            await cleanup.execute(
                delete(Organization).where(Organization.slug.in_(["race-one", "race-two"]))
            )
            await cleanup.commit()

    assert sorted(statuses) == [201, 409]
