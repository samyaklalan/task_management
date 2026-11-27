"""M5a: USR-1/2 /users/me, USR-3 GET /users, USR-4 POST /users, end to end."""

from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.permissions import Feature
from app.core.security import create_access_token, hash_token
from app.models import Organization, PasswordResetToken, RolePermission, User, UserRole
from app.services.email import get_email_sender
from tests.factories import make_organization, make_project, make_role, make_user
from tests.utils.query_counter import count_queries

REGISTER = {
    "organization_name": "Acme Corp",
    "organization_slug": "acme",
    "name": "Asha Rao",
    "email": "asha@acme.com",
    "password": "correct-horse-battery",
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class RecordingEmailSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send_password_reset(self, *, to: str, link: str) -> None:
        self.sent.append({"kind": "reset", "to": to, "link": link})

    async def send_invitation(self, *, to: str, link: str) -> None:
        self.sent.append({"kind": "invitation", "to": to, "link": link})


@pytest.fixture
def outbox(app: FastAPI) -> RecordingEmailSender:
    sender = RecordingEmailSender()
    app.dependency_overrides[get_email_sender] = lambda: sender
    return sender


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> dict[str, Any]:
    """Asha signs up (org Admin). ``headers`` is her auth header."""
    response = await client.post("/auth/register", json=REGISTER)
    assert response.status_code == 201
    body = response.json()
    return {"id": body["user"]["id"], "headers": bearer(body["access_token"])}


@pytest.fixture
async def acme(db_session: AsyncSession, admin: dict[str, Any]) -> Organization:
    return await db_session.scalar(select(Organization).where(Organization.slug == "acme"))


async def colleague(
    session: AsyncSession, organization: Organization, **values: Any
) -> tuple[User, dict[str, str]]:
    """A user in the same organization (no role unless given), with an auth header."""
    user = await make_user(session, organization=organization, **values)
    return user, bearer(create_access_token(user.id, organization.id))


# ── USR-1 GET /users/me ───────────────────────────────────────────────────────


async def test_me_as_admin(client: httpx.AsyncClient, admin: dict[str, Any]) -> None:
    response = await client.get("/users/me", headers=admin["headers"])

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == admin["id"]
    assert (body["name"], body["email"]) == ("Asha Rao", "asha@acme.com")
    assert body["organization"] == {"name": "Acme Corp", "slug": "acme"}
    assert body["org_role"]["name"] == "Admin"
    all_on = {"view": True, "create": True, "edit": True, "delete": True}
    assert body["org_permissions"] == {
        "organization": all_on,
        "users": all_on,
        "roles": all_on,
        "projects": all_on,
    }


async def test_me_with_only_a_project_role_has_no_org_permissions(
    client: httpx.AsyncClient, db_session: AsyncSession, acme: Organization
) -> None:
    user, headers = await colleague(db_session, acme)
    role = await make_role(db_session, organization=acme, name="Developer")
    db_session.add(RolePermission(role_id=role.id, feature="projects", view_perm=True))
    project = await make_project(db_session, organization=acme)
    db_session.add(UserRole(user_id=user.id, role_id=role.id, project_id=project.id))
    await db_session.flush()

    body = (await client.get("/users/me", headers=headers)).json()

    assert body["org_role"] is None
    none_on = {"view": False, "create": False, "edit": False, "delete": False}
    assert all(flags == none_on for flags in body["org_permissions"].values())


async def test_me_with_a_partial_org_role(
    client: httpx.AsyncClient, db_session: AsyncSession, acme: Organization
) -> None:
    user, headers = await colleague(db_session, acme)
    role = await make_role(db_session, organization=acme, name="Project lead")
    db_session.add(
        RolePermission(role_id=role.id, feature="projects", view_perm=True, create_perm=True)
    )
    db_session.add(UserRole(user_id=user.id, role_id=role.id))
    await db_session.flush()

    body = (await client.get("/users/me", headers=headers)).json()

    assert body["org_role"] == {"id": role.id, "name": "Project lead"}
    assert body["org_permissions"]["projects"] == {
        "view": True,
        "create": True,
        "edit": False,
        "delete": False,
    }
    assert body["org_permissions"]["users"]["view"] is False


async def test_me_costs_three_queries(
    client: httpx.AsyncClient, engine: AsyncEngine, admin: dict[str, Any]
) -> None:
    with count_queries(engine) as queries:
        await client.get("/users/me", headers=admin["headers"])

    # user (auth) + org-wide permissions + organization
    assert queries.count == 3, str(queries)


async def test_me_needs_a_login(client: httpx.AsyncClient) -> None:
    assert (await client.get("/users/me")).status_code == 401


# ── USR-2 PATCH /users/me ─────────────────────────────────────────────────────


async def test_change_own_name(client: httpx.AsyncClient, admin: dict[str, Any]) -> None:
    response = await client.patch("/users/me", headers=admin["headers"], json={"name": " Asha R. "})

    assert response.status_code == 200
    assert response.json()["name"] == "Asha R."
    assert (await client.get("/users/me", headers=admin["headers"])).json()["name"] == "Asha R."


async def test_empty_patch_changes_nothing(
    client: httpx.AsyncClient, admin: dict[str, Any]
) -> None:
    response = await client.patch("/users/me", headers=admin["headers"], json={})

    assert response.status_code == 200
    assert response.json()["name"] == "Asha Rao"


@pytest.mark.parametrize(
    "body",
    [{"name": None}, {"name": ""}, {"email": "new@acme.com"}, {"is_active": False}],
    ids=["null", "blank", "email-not-editable", "is_active-not-editable"],
)
async def test_invalid_me_patch(
    client: httpx.AsyncClient, admin: dict[str, Any], body: dict[str, Any]
) -> None:
    response = await client.patch("/users/me", headers=admin["headers"], json=body)

    assert response.status_code == 422


# ── USR-3 GET /users ──────────────────────────────────────────────────────────


@pytest.fixture
async def team(db_session: AsyncSession, acme: Organization) -> dict[str, User]:
    """Four more people in Acme, one inactive, one with an org role; plus a
    user in another organization who must never appear."""

    async def person(name: str, email: str, **values: Any) -> User:
        return await make_user(db_session, organization=acme, name=name, email=email, **values)

    people = {
        "ravi": await person("Ravi Kumar", "ravi@acme.com"),
        "anil": await person("anil sharma", "anil@acme.com"),
        "zoe": await person("Zoe 50%_off", "zoe@acme.com"),
        "old": await person("Old Timer", "old@acme.com", is_active=False),
    }
    manager = await make_role(db_session, organization=acme, name="Manager")
    db_session.add(UserRole(user_id=people["ravi"].id, role_id=manager.id))
    other_org = await make_organization(db_session)
    await make_user(
        db_session, organization=other_org, name="Ravi Outsider", email="ravi@other.com"
    )
    await db_session.flush()
    return people


async def names(client: httpx.AsyncClient, headers: dict, query: str = "") -> list[str]:
    response = await client.get(f"/users{query}", headers=headers)
    assert response.status_code == 200, response.text
    return [item["name"] for item in response.json()["items"]]


async def test_list_shows_only_my_organization_sorted_by_name(
    client: httpx.AsyncClient, admin: dict[str, Any], team: dict[str, User]
) -> None:
    response = await client.get("/users", headers=admin["headers"])

    body = response.json()
    assert [item["name"] for item in body["items"]] == [
        "anil sharma",  # case-insensitive: before "Asha"
        "Asha Rao",
        "Old Timer",
        "Ravi Kumar",
        "Zoe 50%_off",
    ]
    assert body["pagination"] == {"page": 1, "page_size": 20, "total_items": 5, "total_pages": 1}
    ravi = next(item for item in body["items"] if item["name"] == "Ravi Kumar")
    assert set(ravi) == {"id", "name", "email", "is_active", "org_role", "created_at", "updated_at"}
    assert ravi["org_role"]["name"] == "Manager"
    assert next(i for i in body["items"] if i["name"] == "anil sharma")["org_role"] is None


async def test_filter_search_sort_and_page_together(
    client: httpx.AsyncClient, admin: dict[str, Any], team: dict[str, User]
) -> None:
    headers = admin["headers"]
    assert await names(client, headers, "?is_active=false") == ["Old Timer"]
    assert await names(client, headers, "?q=RAVI") == ["Ravi Kumar"]  # not the outsider
    assert await names(client, headers, "?q=acme.com&is_active=true&sort=email&order=desc") == [
        "Zoe 50%_off",
        "Ravi Kumar",
        "Asha Rao",
        "anil sharma",
    ]
    page_2 = await client.get("/users?page=2&page_size=2", headers=headers)
    assert [i["name"] for i in page_2.json()["items"]] == ["Old Timer", "Ravi Kumar"]
    assert page_2.json()["pagination"]["total_pages"] == 3


async def test_search_treats_percent_and_underscore_as_text(
    client: httpx.AsyncClient, admin: dict[str, Any], team: dict[str, User]
) -> None:
    assert await names(client, admin["headers"], "?q=50%25_o") == ["Zoe 50%_off"]
    assert await names(client, admin["headers"], "?q=%25%25%25") == []  # not "match all"


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("?q=ra", "search_too_short"),
        ("?q=%20%20r%20", "search_too_short"),  # trimmed to 1 character
        ("?sort=password_hash", "validation_error"),
        ("?order=sideways", "validation_error"),
        ("?is_active=maybe", "validation_error"),
        ("?q=" + "x" * 101, "validation_error"),
    ],
)
async def test_invalid_list_parameters(
    client: httpx.AsyncClient, admin: dict[str, Any], query: str, code: str
) -> None:
    response = await client.get(f"/users{query}", headers=admin["headers"])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == code


async def test_blank_search_is_ignored(
    client: httpx.AsyncClient, admin: dict[str, Any], team: dict[str, User]
) -> None:
    assert len(await names(client, admin["headers"], "?q=%20%20")) == 5


async def test_list_needs_users_view(
    client: httpx.AsyncClient, db_session: AsyncSession, acme: Organization
) -> None:
    _, headers = await colleague(db_session, acme)

    response = await client.get("/users", headers=headers)

    assert response.status_code == 403


async def test_list_query_count_does_not_grow_with_users(
    client: httpx.AsyncClient, engine: AsyncEngine, admin: dict[str, Any], team: dict[str, User]
) -> None:
    with count_queries(engine) as queries:
        await client.get("/users", headers=admin["headers"])

    # user + permissions + COUNT + one page query (roles joined in): no N+1.
    assert queries.count == 4, str(queries)


# ── USR-4 POST /users ─────────────────────────────────────────────────────────


async def test_create_user_sends_an_invitation(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    outbox: RecordingEmailSender,
    admin: dict[str, Any],
    acme: Organization,
) -> None:
    response = await client.post(
        "/users", headers=admin["headers"], json={"name": "Ravi Kumar", "email": "Ravi@Acme.com"}
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["name"], body["email"], body["is_active"], body["org_role"]) == (
        "Ravi Kumar",
        "ravi@acme.com",
        True,
        None,
    )
    created = await db_session.get(User, body["id"])
    assert created.organization_id == acme.id
    [email] = outbox.sent
    assert (email["kind"], email["to"]) == ("invitation", "ravi@acme.com")
    token = parse_qs(urlsplit(email["link"]).query)["token"][0]
    stored = await db_session.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_token(token))
    )
    assert stored.user_id == body["id"] and stored.used_at is None


async def test_new_user_cannot_log_in_until_they_set_a_password(
    client: httpx.AsyncClient, outbox: RecordingEmailSender, admin: dict[str, Any]
) -> None:
    await client.post(
        "/users", headers=admin["headers"], json={"name": "Ravi", "email": "ravi@acme.com"}
    )
    token = parse_qs(urlsplit(outbox.sent[0]["link"]).query)["token"][0]

    for guess in ("", "password", "ravi@acme.com"):
        login = await client.post(
            "/auth/login", json={"email": "ravi@acme.com", "password": guess or "x"}
        )
        assert login.status_code == 401
    confirm = await client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": "ravis-own-pass"}
    )
    login = await client.post(
        "/auth/login", json={"email": "ravi@acme.com", "password": "ravis-own-pass"}
    )
    assert confirm.status_code == 204
    assert login.status_code == 200


async def test_create_user_with_a_taken_email(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    outbox: RecordingEmailSender,
    admin: dict[str, Any],
) -> None:
    # Taken in ANY organization: one email = one account (P-8).
    other = await make_organization(db_session)
    await make_user(db_session, organization=other, email="ravi@other.com")

    response = await client.post(
        "/users", headers=admin["headers"], json={"name": "Ravi", "email": "RAVI@other.com"}
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"] == [
        {"field": "email", "message": "An account with this email already exists."}
    ]
    assert outbox.sent == []


async def test_create_user_needs_users_create(
    client: httpx.AsyncClient, db_session: AsyncSession, acme: Organization
) -> None:
    viewer, headers = await colleague(db_session, acme)
    role = await make_role(db_session, organization=acme, name="HR viewer")
    db_session.add(RolePermission(role_id=role.id, feature=Feature.USERS.value, view_perm=True))
    db_session.add(UserRole(user_id=viewer.id, role_id=role.id))
    await db_session.flush()

    can_list = await client.get("/users", headers=headers)
    can_create = await client.post(
        "/users", headers=headers, json={"name": "Someone", "email": "someone@acme.com"}
    )

    assert can_list.status_code == 200
    assert can_create.status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {"name": "Ravi"},
        {"email": "ravi@acme.com"},
        {"name": "Ravi", "email": "not-an-email"},
        {"name": "Ravi", "email": "ravi@acme.com", "password": "set-by-admin"},
    ],
    ids=["no-email", "no-name", "bad-email", "password-not-accepted"],
)
async def test_invalid_create_body(
    client: httpx.AsyncClient, admin: dict[str, Any], body: dict[str, Any]
) -> None:
    assert (await client.post("/users", headers=admin["headers"], json=body)).status_code == 422


async def test_deactivated_admin_is_locked_out_of_the_list(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, Any]
) -> None:
    await db_session.execute(update(User).where(User.id == admin["id"]).values(is_active=False))

    assert (await client.get("/users", headers=admin["headers"])).status_code == 401
