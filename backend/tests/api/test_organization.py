"""M4: ORG-1 GET /organization and ORG-2 PATCH /organization, end to end."""

from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import create_app
from app.models import Organization, Role, User
from tests.factories import make_user

REGISTER = {
    "organization_name": "Acme Corp",
    "organization_slug": "acme",
    "name": "Asha Rao",
    "email": "asha@acme.com",
    "password": "correct-horse-battery",
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> dict[str, str]:
    """Asha signs up (org Admin); returns her auth header."""
    response = await client.post("/auth/register", json=REGISTER)
    assert response.status_code == 201
    return bearer(response.json()["access_token"])


async def current(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.get("/organization", headers=headers)
    assert response.status_code == 200
    return response.json()


# ── ORG-1 GET ─────────────────────────────────────────────────────────────────


async def test_get_organization(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    body = await current(client, admin)

    assert set(body) == {"name", "slug", "description", "created_at", "updated_at"}  # no id
    assert (body["name"], body["slug"], body["description"]) == ("Acme Corp", "acme", None)
    assert body["created_at"].endswith("Z")


async def test_get_needs_a_login(client: httpx.AsyncClient) -> None:
    assert (await client.get("/organization")).status_code == 401


async def test_member_without_organization_permission_gets_403(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, str]
) -> None:
    organization = await db_session.scalar(select(Organization))
    colleague = await make_user(db_session, organization=organization)  # no role
    headers = bearer(create_access_token(colleague.id, organization.id))

    response = await client.get("/organization", headers=headers)
    patch = await client.patch(
        "/organization",
        headers=headers,
        json={"name": "Hacked", "expected_updated_at": "2026-01-01T00:00:00Z"},
    )

    assert response.status_code == 403
    assert patch.status_code == 403
    assert (await current(client, admin))["name"] == "Acme Corp"


# ── ORG-2 PATCH ───────────────────────────────────────────────────────────────


async def test_rename_and_describe(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    before = await current(client, admin)

    response = await client.patch(
        "/organization",
        headers=admin,
        json={
            "name": "  Acme Corporation ",
            "description": "Product engineering",
            "expected_updated_at": before["updated_at"],  # exactly as received
        },
    )

    assert response.status_code == 200
    after = response.json()
    assert after["name"] == "Acme Corporation"  # trimmed
    assert after["description"] == "Product engineering"
    assert after["slug"] == "acme"  # never changes (A-8)
    assert after["updated_at"] > before["updated_at"]
    assert after["created_at"] == before["created_at"]
    assert await current(client, admin) == after


async def test_only_the_sent_fields_change(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    first = (
        await client.patch(
            "/organization",
            headers=admin,
            json={
                "description": "Keep me",
                "expected_updated_at": (await current(client, admin))["updated_at"],
            },
        )
    ).json()

    second = await client.patch(
        "/organization",
        headers=admin,
        json={"name": "New name", "expected_updated_at": first["updated_at"]},
    )

    assert second.json()["description"] == "Keep me"


@pytest.mark.parametrize("cleared", [None, "", "   "])
async def test_description_can_be_cleared(
    client: httpx.AsyncClient, admin: dict[str, str], cleared: str | None
) -> None:
    described = (
        await client.patch(
            "/organization",
            headers=admin,
            json={
                "description": "Something",
                "expected_updated_at": (await current(client, admin))["updated_at"],
            },
        )
    ).json()

    response = await client.patch(
        "/organization",
        headers=admin,
        json={"description": cleared, "expected_updated_at": described["updated_at"]},
    )

    assert response.json()["description"] is None


async def test_stale_update_is_refused_with_the_current_version(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    seen = await current(client, admin)  # two people open the settings page
    first = await client.patch(
        "/organization",
        headers=admin,
        json={"name": "First save", "expected_updated_at": seen["updated_at"]},
    )

    second = await client.patch(
        "/organization",
        headers=admin,
        json={"name": "Second save", "expected_updated_at": seen["updated_at"]},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "stale_update"
    assert error["details"]["current"] == first.json()  # the page can reload from this
    assert (await current(client, admin))["name"] == "First save"  # not overwritten


async def test_patch_with_no_changes(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    seen = await current(client, admin)

    fresh = await client.patch(
        "/organization", headers=admin, json={"expected_updated_at": seen["updated_at"]}
    )
    stale = await client.patch(
        "/organization", headers=admin, json={"expected_updated_at": "2020-01-01T00:00:00Z"}
    )

    assert fresh.status_code == 200 and fresh.json() == seen  # nothing changed
    assert stale.status_code == 409


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"slug": "new-slug"}, "slug"),  # can't be changed (A-8)
        ({"name": None}, "name"),
        ({"name": "   "}, "name"),
        ({"name": "x" * 151}, "name"),
        ({"description": "x" * 2001}, "description"),
        ({"expected_updated_at": "2026-11-16T07:30:00"}, "expected_updated_at"),  # no zone
    ],
)
async def test_invalid_patch_body(
    client: httpx.AsyncClient, admin: dict[str, str], body: dict[str, Any], field: str
) -> None:
    payload = {"expected_updated_at": "2026-11-16T07:30:00Z"} | body

    response = await client.patch("/organization", headers=admin, json=payload)

    assert response.status_code == 422
    assert [d["field"] for d in response.json()["error"]["details"]] == [field]


async def test_expected_updated_at_is_required(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    response = await client.patch("/organization", headers=admin, json={"name": "X"})

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "expected_updated_at"


# ── Two admins saving at the same moment (real, separate transactions) ────────


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
            org_ids = select(Organization.id).where(Organization.slug == "race-org")
            await cleanup.execute(delete(User).where(User.organization_id.in_(org_ids)))
            await cleanup.execute(delete(Role).where(Role.organization_id.in_(org_ids)))
            await cleanup.execute(delete(Organization).where(Organization.slug == "race-org"))
            await cleanup.commit()


async def test_simultaneous_saves_only_one_wins(real_client: httpx.AsyncClient) -> None:
    signed_up = await real_client.post(
        "/auth/register", json=REGISTER | {"organization_slug": "race-org"}
    )
    headers = bearer(signed_up.json()["access_token"])
    seen = (await real_client.get("/organization", headers=headers)).json()
    statuses: list[int] = []

    async def save(name: str) -> None:
        body = {"name": name, "expected_updated_at": seen["updated_at"]}
        response = await real_client.patch("/organization", headers=headers, json=body)
        statuses.append(response.status_code)

    async with anyio.create_task_group() as group:
        group.start_soon(save, "Name A")
        group.start_soon(save, "Name B")

    assert sorted(statuses) == [200, 409]
