"""User and token repositories against PostgreSQL
(B25: app/repositories/user.py, app/repositories/token.py)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.security import generate_token, hash_token
from app.models import Organization, PasswordResetToken, RefreshToken, User
from app.repositories import token as token_repository
from app.repositories import user as user_repository
from tests.factories import PLACEHOLDER_PASSWORD_HASH, make_organization, make_user
from tests.utils.query_counter import count_queries

NOW = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)
IN_14_DAYS = NOW + timedelta(days=14)

# ── Users ─────────────────────────────────────────────────────────────────────


async def test_create_and_find_a_user(db_session: AsyncSession) -> None:
    organization = await make_organization(db_session)
    created = await user_repository.create(
        db_session,
        organization_id=organization.id,
        name="Asha Rao",
        email="asha@acme.com",
        password_hash=PLACEHOLDER_PASSWORD_HASH,
    )

    assert created.id is not None and created.is_active  # server default is loaded
    assert await user_repository.get_by_id(db_session, created.id) is created
    assert await user_repository.get_by_email(db_session, "asha@acme.com") is created
    assert await user_repository.get_by_email(db_session, "nobody@acme.com") is None
    assert await user_repository.get_by_id(db_session, 10**12) is None


async def test_duplicate_email_raises_integrity_error(db_session: AsyncSession) -> None:
    existing = await make_user(db_session, email="asha@acme.com")

    with pytest.raises(IntegrityError, match="uq_users_email"):
        await user_repository.create(
            db_session,
            organization_id=existing.organization_id,
            name="Someone else",
            email="asha@acme.com",
            password_hash=PLACEHOLDER_PASSWORD_HASH,
        )


async def test_normal_lookup_never_loads_the_password_hash(
    engine: AsyncEngine, db_session: AsyncSession
) -> None:
    await make_user(db_session, email="asha@acme.com")
    db_session.expunge_all()  # start from an empty session, as a new request would

    with count_queries(engine) as queries:
        user = await user_repository.get_by_email(db_session, "asha@acme.com")

    assert "password_hash" not in queries.statements[0]
    with pytest.raises(InvalidRequestError, match="password_hash"):
        _ = user.password_hash  # deferred + raiseload: no hidden extra query


async def test_login_lookup_loads_the_password_hash_in_one_query(
    engine: AsyncEngine, db_session: AsyncSession
) -> None:
    await make_user(db_session, email="asha@acme.com", password_hash="$argon2id$stored")
    db_session.expunge_all()

    with count_queries(engine) as queries:
        user = await user_repository.get_by_email_with_password(db_session, "asha@acme.com")
        password_hash = user.password_hash

    assert password_hash == "$argon2id$stored"
    assert queries.count == 1, str(queries)


async def test_set_password_hash_updates_hash_and_updated_at(db_session: AsyncSession) -> None:
    user = await make_user(db_session, email="asha@acme.com")
    await db_session.commit()  # a new transaction, so statement_timestamp() moves on
    before = user.updated_at

    await user_repository.set_password_hash(db_session, user.id, "$argon2id$new")

    stored = await db_session.execute(
        select(User.password_hash, User.updated_at).where(User.id == user.id)
    )
    password_hash, updated_at = stored.one()
    assert password_hash == "$argon2id$new"
    assert updated_at > before


# ── Refresh tokens ────────────────────────────────────────────────────────────


async def test_refresh_token_life_cycle(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    raw, token_hash = generate_token()
    created = await token_repository.create_refresh_token(
        db_session, user_id=user.id, token_hash=token_hash, expires_at=IN_14_DAYS
    )

    found = await token_repository.get_refresh_token_for_update(db_session, hash_token(raw))
    assert found is not None and found.id == created.id
    assert (found.revoked_at, found.last_used_at) == (None, None)

    await token_repository.revoke_refresh_token(db_session, found.id, NOW, used=True)
    rotated = await token_repository.get_refresh_token_for_update(db_session, token_hash)
    assert (rotated.revoked_at, rotated.last_used_at) == (NOW, NOW)

    # Revoking again keeps the first revocation time (the 30 s grace window
    # of api.md section 2.2 is measured from it).
    await token_repository.revoke_refresh_token(db_session, found.id, NOW + timedelta(hours=1))
    again = await token_repository.get_refresh_token_for_update(db_session, token_hash)
    assert again.revoked_at == NOW


async def test_lookup_returns_revoked_and_expired_tokens_too(db_session: AsyncSession) -> None:
    # The service must see a revoked token to detect reuse (section 2.2).
    user = await make_user(db_session)
    _, token_hash = generate_token()
    await token_repository.create_refresh_token(
        db_session, user_id=user.id, token_hash=token_hash, expires_at=NOW - timedelta(days=1)
    )
    await token_repository.revoke_refresh_token_by_hash(db_session, token_hash, NOW)

    found = await token_repository.get_refresh_token_for_update(db_session, token_hash)
    assert found is not None and found.revoked_at == NOW


async def test_unknown_token_is_none(db_session: AsyncSession) -> None:
    assert await token_repository.get_refresh_token_for_update(db_session, "0" * 64) is None
    assert await token_repository.get_reset_token_for_update(db_session, "0" * 64) is None


async def test_logout_of_an_unknown_token_changes_nothing(db_session: AsyncSession) -> None:
    await token_repository.revoke_refresh_token_by_hash(db_session, "0" * 64, NOW)  # no error


async def test_revoke_all_only_touches_this_users_active_sessions(
    db_session: AsyncSession,
) -> None:
    user = await make_user(db_session)
    other_user = await make_user(db_session)
    hashes = [generate_token()[1] for _ in range(4)]
    for token_hash in hashes[:3]:
        await token_repository.create_refresh_token(
            db_session, user_id=user.id, token_hash=token_hash, expires_at=IN_14_DAYS
        )
    await token_repository.create_refresh_token(
        db_session, user_id=other_user.id, token_hash=hashes[3], expires_at=IN_14_DAYS
    )
    earlier = NOW - timedelta(hours=1)
    await token_repository.revoke_refresh_token_by_hash(db_session, hashes[0], earlier)

    revoked = await token_repository.revoke_all_refresh_tokens(db_session, user.id, NOW)

    assert revoked == 2  # the one already revoked isn't counted or changed
    rows = await db_session.execute(
        select(RefreshToken.token_hash, RefreshToken.revoked_at).order_by(RefreshToken.id)
    )
    assert [revoked_at for _hash, revoked_at in rows] == [earlier, NOW, NOW, None]


# ── Password-reset tokens ─────────────────────────────────────────────────────


async def test_reset_token_is_used_once(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    _, token_hash = generate_token()
    token = await token_repository.create_reset_token(
        db_session, user_id=user.id, token_hash=token_hash, expires_at=NOW + timedelta(minutes=30)
    )

    await token_repository.mark_reset_token_used(db_session, token.id, NOW)
    await token_repository.mark_reset_token_used(db_session, token.id, NOW + timedelta(minutes=5))

    found = await token_repository.get_reset_token_for_update(db_session, token_hash)
    assert found.used_at == NOW  # the first use is kept


async def test_new_reset_link_invalidates_older_unused_ones(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    other_user = await make_user(db_session)
    for owner in (user, user, other_user):
        await token_repository.create_reset_token(
            db_session,
            user_id=owner.id,
            token_hash=generate_token()[1],
            expires_at=NOW + timedelta(minutes=30),
        )

    invalidated = await token_repository.invalidate_reset_tokens(db_session, user.id, NOW)

    assert invalidated == 2
    used = await db_session.scalars(
        select(PasswordResetToken.used_at).order_by(PasswordResetToken.id)
    )
    assert list(used) == [NOW, NOW, None]  # the other user's link still works


# ── Row locking (needs committed data and two connections) ────────────────────


@pytest.fixture
async def committed_refresh_token(engine: AsyncEngine) -> AsyncIterator[str]:
    """A user and a refresh token committed for real, so a second connection
    can see them. Removed again after the test."""
    _, token_hash = generate_token()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        user = await make_user(session)
        await token_repository.create_refresh_token(
            session, user_id=user.id, token_hash=token_hash, expires_at=IN_14_DAYS
        )
        await session.commit()
    try:
        yield token_hash
    finally:
        async with AsyncSession(engine) as session:
            await session.execute(delete(User).where(User.id == user.id))  # tokens cascade
            await session.execute(
                delete(Organization).where(Organization.id == user.organization_id)
            )
            await session.commit()


async def test_for_update_lock_makes_a_second_request_wait(
    engine: AsyncEngine, committed_refresh_token: str
) -> None:
    async with AsyncSession(engine) as first, AsyncSession(engine) as second:
        locked = await token_repository.get_refresh_token_for_update(first, committed_refresh_token)
        assert locked is not None

        # The second request would wait until the first one commits; give up
        # after 200 ms so the test can see that it was really blocked.
        await second.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(DBAPIError, match="lock timeout") as caught:
            await token_repository.get_refresh_token_for_update(second, committed_refresh_token)
        assert caught.value.orig.sqlstate == "55P03"  # lock_not_available

        await first.rollback()
