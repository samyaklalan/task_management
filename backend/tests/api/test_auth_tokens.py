"""M2: AUTH-3 refresh, AUTH-4 logout, AUTH-5/6 password reset, end to end."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.security import hash_token
from app.db.session import get_session
from app.main import create_app
from app.models import Organization, PasswordResetToken, RefreshToken, Role, User
from app.services.email import get_email_sender

REGISTER = {
    "organization_name": "Acme Corp",
    "name": "Asha Rao",
    "email": "asha@acme.com",
    "password": "correct-horse-battery",
}
LOGIN = {"email": "asha@acme.com", "password": "correct-horse-battery"}


class RecordingEmailSender:
    """Collects the emails instead of sending them."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send_password_reset(self, *, to: str, link: str) -> None:
        self.sent.append({"to": to, "link": link})


@pytest.fixture
def outbox(app: FastAPI) -> RecordingEmailSender:
    sender = RecordingEmailSender()
    app.dependency_overrides[get_email_sender] = lambda: sender
    return sender


@pytest.fixture
async def asha(client: httpx.AsyncClient) -> dict[str, Any]:
    """A signed-up user; the response includes their first refresh token."""
    response = await client.post("/auth/register", json=REGISTER)
    assert response.status_code == 201
    return response.json()


async def refresh_row(session: AsyncSession, token: str) -> RefreshToken:
    return await session.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == hash_token(token))
        .execution_options(populate_existing=True)
    )


async def active_sessions(session: AsyncSession, user_id: int) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
    )


async def make_older(session: AsyncSession, token: str, *, seconds: int) -> None:
    """Pretend the token was revoked ``seconds`` ago."""
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == hash_token(token))
        .values(revoked_at=RefreshToken.revoked_at - timedelta(seconds=seconds))
    )


# ── AUTH-3 refresh ────────────────────────────────────────────────────────────


async def test_refresh_rotates_the_token(
    client: httpx.AsyncClient, db_session: AsyncSession, asha: dict[str, Any]
) -> None:
    old = asha["refresh_token"]

    response = await client.post("/auth/refresh", json={"refresh_token": old})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {
        "access_token",
        "token_type",
        "expires_in",
        "refresh_token",
        "refresh_expires_in",
    }
    assert body["refresh_token"] != old
    old_row = await refresh_row(db_session, old)
    assert old_row.revoked_at is not None and old_row.last_used_at == old_row.revoked_at
    assert (await refresh_row(db_session, body["refresh_token"])).revoked_at is None
    # The new token can be rotated in turn.
    again = await client.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert again.status_code == 200


async def test_same_token_twice_within_30_seconds_is_a_retry_not_an_attack(
    client: httpx.AsyncClient, db_session: AsyncSession, asha: dict[str, Any]
) -> None:
    first = await client.post("/auth/refresh", json={"refresh_token": asha["refresh_token"]})

    second = await client.post("/auth/refresh", json={"refresh_token": asha["refresh_token"]})

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "token_already_rotated"
    # Nothing else was revoked: the token from the first refresh still works.
    assert await active_sessions(db_session, asha["user"]["id"]) == 1
    third = await client.post(
        "/auth/refresh", json={"refresh_token": first.json()["refresh_token"]}
    )
    assert third.status_code == 200


async def test_reusing_an_old_rotated_token_logs_out_everywhere(
    client: httpx.AsyncClient, db_session: AsyncSession, asha: dict[str, Any]
) -> None:
    stolen = asha["refresh_token"]
    current = (await client.post("/auth/refresh", json={"refresh_token": stolen})).json()
    await client.post("/auth/login", json=LOGIN)
    assert await active_sessions(db_session, asha["user"]["id"]) == 2
    await make_older(db_session, stolen, seconds=31)

    replay = await client.post("/auth/refresh", json={"refresh_token": stolen})

    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "token_reused"
    assert await active_sessions(db_session, asha["user"]["id"]) == 0
    # Even the legitimate newest token stopped working: the user logs in again.
    after = await client.post("/auth/refresh", json={"refresh_token": current["refresh_token"]})
    assert after.status_code == 401


@pytest.mark.parametrize("problem", ["unknown", "expired", "logged-out", "inactive-user"])
async def test_refresh_with_an_invalid_token(
    client: httpx.AsyncClient, db_session: AsyncSession, asha: dict[str, Any], problem: str
) -> None:
    token = asha["refresh_token"]
    if problem == "unknown":
        token = "not-a-real-token"
    elif problem == "expired":
        await db_session.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == hash_token(token))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    elif problem == "logged-out":
        await client.post("/auth/logout", json={"refresh_token": token})
        await make_older(db_session, token, seconds=3600)
    elif problem == "inactive-user":
        await db_session.execute(
            update(User).where(User.id == asha["user"]["id"]).values(is_active=False)
        )

    response = await client.post("/auth/refresh", json={"refresh_token": token})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_a_logged_out_token_never_triggers_reuse_detection(
    client: httpx.AsyncClient, db_session: AsyncSession, asha: dict[str, Any]
) -> None:
    """After a password reset all old sessions are revoked. A device that still
    has one must not log out the session the user started afterwards."""
    await client.post("/auth/logout", json={"refresh_token": asha["refresh_token"]})
    await make_older(db_session, asha["refresh_token"], seconds=3600)
    await client.post("/auth/login", json=LOGIN)

    response = await client.post("/auth/refresh", json={"refresh_token": asha["refresh_token"]})

    assert response.json()["error"]["code"] == "invalid_token"
    assert await active_sessions(db_session, asha["user"]["id"]) == 1  # new login untouched


# ── AUTH-4 logout ─────────────────────────────────────────────────────────────


async def test_logout_revokes_only_this_session(
    client: httpx.AsyncClient, db_session: AsyncSession, asha: dict[str, Any]
) -> None:
    await client.post("/auth/login", json=LOGIN)

    response = await client.post("/auth/logout", json={"refresh_token": asha["refresh_token"]})

    assert response.status_code == 204
    assert response.content == b""
    assert (await refresh_row(db_session, asha["refresh_token"])).revoked_at is not None
    assert await active_sessions(db_session, asha["user"]["id"]) == 1  # the other device


@pytest.mark.parametrize("token", ["never-issued", None])
async def test_logout_is_always_204(
    client: httpx.AsyncClient, asha: dict[str, Any], token: str | None
) -> None:
    token = token or asha["refresh_token"]
    for _ in range(2):  # repeated clicks are harmless
        response = await client.post("/auth/logout", json={"refresh_token": token})
        assert response.status_code == 204


# ── AUTH-5 password-reset request ─────────────────────────────────────────────


def token_from(link: str) -> str:
    return parse_qs(urlsplit(link).query)["token"][0]


async def test_reset_request_for_an_existing_user_sends_a_link(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    outbox: RecordingEmailSender,
    asha: dict[str, Any],
) -> None:
    response = await client.post("/auth/password-reset/request", json={"email": "Asha@Acme.com"})

    assert response.status_code == 202
    assert response.json() == {
        "message": "If an account exists for this email, a reset link has been sent."
    }
    [email] = outbox.sent
    assert email["to"] == "asha@acme.com"
    assert email["link"].startswith("http://localhost:3000/reset-password?token=")
    stored = await db_session.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == hash_token(token_from(email["link"]))
        )
    )
    assert stored.used_at is None
    assert timedelta(minutes=29) < stored.expires_at - datetime.now(UTC) <= timedelta(minutes=30)


async def test_a_new_link_cancels_the_older_one(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    outbox: RecordingEmailSender,
    asha: dict[str, Any],
) -> None:
    for _ in range(2):
        await client.post("/auth/password-reset/request", json={"email": "asha@acme.com"})
    first, second = (token_from(email["link"]) for email in outbox.sent)

    old = await client.post(
        "/auth/password-reset/confirm", json={"token": first, "new_password": "brand-new-pass"}
    )
    new = await client.post(
        "/auth/password-reset/confirm", json={"token": second, "new_password": "brand-new-pass"}
    )

    assert old.status_code == 401
    assert new.status_code == 204


@pytest.mark.parametrize("email", ["nobody@acme.com", "inactive@acme.com"])
async def test_reset_request_gives_the_same_answer_without_sending(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    outbox: RecordingEmailSender,
    asha: dict[str, Any],
    email: str,
) -> None:
    if email == "inactive@acme.com":
        await db_session.execute(
            update(User)
            .where(User.id == asha["user"]["id"])
            .values(email="inactive@acme.com", is_active=False)
        )

    response = await client.post("/auth/password-reset/request", json={"email": email})

    assert response.status_code == 202
    assert response.json()["message"].startswith("If an account exists")
    assert outbox.sent == []
    assert await db_session.scalar(select(func.count()).select_from(PasswordResetToken)) == 0


# ── AUTH-6 password-reset confirm ─────────────────────────────────────────────


@pytest.fixture
async def reset_token(
    client: httpx.AsyncClient, outbox: RecordingEmailSender, asha: dict[str, Any]
) -> str:
    await client.post("/auth/password-reset/request", json={"email": "asha@acme.com"})
    return token_from(outbox.sent[-1]["link"])


async def test_confirm_sets_the_password_and_logs_out_everywhere(
    client: httpx.AsyncClient, db_session: AsyncSession, asha: dict[str, Any], reset_token: str
) -> None:
    response = await client.post(
        "/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": "another-long-password"},
    )

    assert response.status_code == 204
    assert await active_sessions(db_session, asha["user"]["id"]) == 0
    old_login = await client.post("/auth/login", json=LOGIN)
    new_login = await client.post(
        "/auth/login", json={"email": "asha@acme.com", "password": "another-long-password"}
    )
    assert old_login.status_code == 401
    assert new_login.status_code == 200


async def test_a_reset_link_works_only_once(client: httpx.AsyncClient, reset_token: str) -> None:
    body = {"token": reset_token, "new_password": "another-long-password"}

    first = await client.post("/auth/password-reset/confirm", json=body)
    second = await client.post("/auth/password-reset/confirm", json=body)

    assert first.status_code == 204
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "invalid_token"


@pytest.mark.parametrize("problem", ["unknown", "expired"])
async def test_confirm_with_an_invalid_token(
    client: httpx.AsyncClient, db_session: AsyncSession, reset_token: str, problem: str
) -> None:
    token = "not-a-real-token" if problem == "unknown" else reset_token
    if problem == "expired":
        await db_session.execute(
            update(PasswordResetToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    response = await client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": "long-enough-pw"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_confirm_checks_the_new_password_rule(
    client: httpx.AsyncClient, reset_token: str
) -> None:
    response = await client.post(
        "/auth/password-reset/confirm", json={"token": reset_token, "new_password": "short"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "new_password"


async def test_console_sender_logs_the_link_unredacted(caplog: pytest.LogCaptureFixture) -> None:
    from app.services.email import ConsoleEmailSender

    await ConsoleEmailSender().send_password_reset(
        to="asha@acme.com", link="http://localhost:3000/reset-password?token=abc123"
    )

    [record] = [r for r in caplog.records if r.name == "app.services.email"]
    assert "token=abc123" in record.getMessage()
    assert record.unredacted is True


# ── Simultaneous requests (real, separate transactions) ───────────────────────


@pytest.fixture
async def real_client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """Each request gets its own session and really commits, like production.
    Everything created under the "race-test" organization is removed afterwards."""
    application = create_app()

    async def separate_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    sender = RecordingEmailSender()
    application.dependency_overrides[get_session] = separate_session
    application.dependency_overrides[get_email_sender] = lambda: sender
    transport = httpx.ASGITransport(app=application)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1") as http:
            http.outbox = sender  # type: ignore[attr-defined]
            yield http
    finally:
        async with AsyncSession(engine) as cleanup:
            org_ids = select(Organization.id).where(Organization.slug == "race-test")
            await cleanup.execute(delete(User).where(User.organization_id.in_(org_ids)))
            await cleanup.execute(delete(Role).where(Role.organization_id.in_(org_ids)))
            await cleanup.execute(delete(Organization).where(Organization.slug == "race-test"))
            await cleanup.commit()


async def _twice_at_once(http: httpx.AsyncClient, url: str, body: dict) -> list[int]:
    statuses: list[int] = []

    async def call() -> None:
        statuses.append((await http.post(url, json=body)).status_code)

    async with anyio.create_task_group() as group:
        group.start_soon(call)
        group.start_soon(call)
    return sorted(statuses)


async def test_two_tabs_refreshing_at_once(real_client: httpx.AsyncClient) -> None:
    signed_up = await real_client.post(
        "/auth/register", json=REGISTER | {"organization_slug": "race-test"}
    )
    body = {"refresh_token": signed_up.json()["refresh_token"]}

    # FOR UPDATE: the second waits for the first, then sees it was just rotated.
    assert await _twice_at_once(real_client, "/auth/refresh", body) == [200, 409]


async def test_two_confirms_with_one_reset_link(real_client: httpx.AsyncClient) -> None:
    await real_client.post("/auth/register", json=REGISTER | {"organization_slug": "race-test"})
    await real_client.post("/auth/password-reset/request", json={"email": "asha@acme.com"})
    token = token_from(real_client.outbox.sent[-1]["link"])  # type: ignore[attr-defined]
    body = {"token": token, "new_password": "another-long-password"}

    assert await _twice_at_once(real_client, "/auth/password-reset/confirm", body) == [204, 401]
