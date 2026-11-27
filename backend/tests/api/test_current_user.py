"""M3: the logged-in user and the org-permission guard (app/api/deps.py).

No real endpoint uses them yet (that starts with M4), so these tests add two
small routes to the test app.
"""

from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.deps import CurrentUserDep, require_org_permission
from app.core.config import get_settings
from app.core.permissions import Action, Feature
from app.core.security import create_access_token
from app.models import Organization, User
from tests.factories import make_user
from tests.utils.query_counter import count_queries

REGISTER = {
    "organization_name": "Acme Corp",
    "name": "Asha Rao",
    "email": "asha@acme.com",
    "password": "correct-horse-battery",
}


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    """The normal test app plus two routes that use the new dependencies."""

    async def whoami(user: CurrentUserDep) -> dict:
        return {"id": user.id, "email": user.email}

    async def users_page() -> dict:
        return {"ok": True}

    app.add_api_route("/api/v1/test/whoami", whoami)
    app.add_api_route(
        "/api/v1/test/users-page",
        users_page,
        dependencies=[
            require_org_permission(Feature.USERS, Action.VIEW),
            require_org_permission(Feature.USERS, Action.EDIT),  # 2 checks, 1 query
        ],
    )
    return app


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> dict:
    """Asha signs up, so she is the org Admin. Returns her login response."""
    response = await client.post("/auth/register", json=REGISTER)
    assert response.status_code == 201
    return response.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── get_current_user ──────────────────────────────────────────────────────────


async def test_valid_token_gives_the_user(client: httpx.AsyncClient, admin: dict) -> None:
    response = await client.get("/test/whoami", headers=bearer(admin["access_token"]))

    assert response.status_code == 200
    assert response.json() == {"id": admin["user"]["id"], "email": "asha@acme.com"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Basic YXNoYTpwYXNzd29yZA=="},
        {"Authorization": "Bearer not-a-jwt"},
    ],
    ids=["no-header", "empty-bearer", "basic-auth", "garbage"],
)
async def test_missing_or_bad_token_is_401(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.get("/test/whoami", headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_expired_token_is_401(client: httpx.AsyncClient, admin: dict) -> None:
    settings = get_settings()
    past = datetime.now(UTC) - timedelta(minutes=20)
    expired = jwt.encode(
        {
            "sub": str(admin["user"]["id"]),
            "org": 1,
            "type": "access",
            "iat": past,
            "exp": past + timedelta(minutes=15),
        },
        settings.jwt_secret_key.get_secret_value(),
        algorithm="HS256",
    )

    response = await client.get("/test/whoami", headers=bearer(expired))

    assert response.status_code == 401


async def test_deactivation_takes_effect_immediately(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict
) -> None:
    token = admin["access_token"]  # still valid for 15 minutes
    await db_session.execute(
        update(User).where(User.id == admin["user"]["id"]).values(is_active=False)
    )

    response = await client.get("/test/whoami", headers=bearer(token))

    assert response.status_code == 401


async def test_token_for_a_user_that_no_longer_exists_is_401(client: httpx.AsyncClient) -> None:
    token = create_access_token(user_id=10**12, organization_id=1)

    response = await client.get("/test/whoami", headers=bearer(token))

    assert response.status_code == 401


async def test_token_with_the_wrong_organization_is_401(
    client: httpx.AsyncClient, admin: dict
) -> None:
    claims_org = create_access_token(user_id=admin["user"]["id"], organization_id=10**12)

    response = await client.get("/test/whoami", headers=bearer(claims_org))

    assert response.status_code == 401


# ── require_org_permission ────────────────────────────────────────────────────


async def test_admin_passes_the_guard_with_one_permission_query(
    client: httpx.AsyncClient, engine: AsyncEngine, admin: dict
) -> None:
    with count_queries(engine) as queries:
        response = await client.get("/test/users-page", headers=bearer(admin["access_token"]))

    assert response.status_code == 200
    # One query for the user, one for the permissions, shared by both checks.
    assert queries.count == 2, str(queries)


async def test_user_without_the_permission_gets_403(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict
) -> None:
    # A colleague in the same organization with no role at all.
    asha = await db_session.get(User, admin["user"]["id"])
    organization = await db_session.get(Organization, asha.organization_id)
    colleague = await make_user(db_session, organization=organization)
    token = create_access_token(colleague.id, colleague.organization_id)

    allowed_in = await client.get("/test/whoami", headers=bearer(token))
    guarded = await client.get("/test/users-page", headers=bearer(token))

    assert allowed_in.status_code == 200  # logged in...
    assert guarded.status_code == 403  # ...but not allowed here
    assert guarded.json()["error"]["code"] == "permission_denied"


async def test_guard_needs_a_login_first(client: httpx.AsyncClient) -> None:
    response = await client.get("/test/users-page")

    assert response.status_code == 401
