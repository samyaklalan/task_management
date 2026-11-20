"""Database access for refresh tokens and password-reset tokens.

Tokens are always looked up by their SHA-256 hash (the raw token is never
stored). The ``..._for_update`` lookups lock the row (``SELECT ... FOR UPDATE``)
until the transaction ends, so two requests with the same token are handled one
after the other, never both at once (refresh rotation, api.md section 2.2;
AUTH-6). They return the row even if it is expired, revoked or used: the
service needs to see that, e.g. to detect a reused refresh token.

``now`` is passed in by the service (one ``datetime.now(UTC)`` per request), so
every time in one request is the same instant and tests can choose the time.
"""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PasswordResetToken, RefreshToken

# ── Refresh tokens ────────────────────────────────────────────────────────────


async def create_refresh_token(
    session: AsyncSession, *, user_id: int, token_hash: str, expires_at: datetime
) -> RefreshToken:
    token = RefreshToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
    session.add(token)
    await session.flush()
    return token


async def get_refresh_token_for_update(
    session: AsyncSession, token_hash: str
) -> RefreshToken | None:
    return await session.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == token_hash)
        .with_for_update()
        # If this row is already in the session, overwrite it with the values
        # read under the lock, not the older ones from before.
        .execution_options(populate_existing=True)
    )


async def revoke_refresh_token(
    session: AsyncSession, token_id: int, now: datetime, *, used: bool = False
) -> None:
    """Revoke one token. ``used=True`` for rotation: it was exchanged, so
    ``last_used_at`` is set too."""
    values: dict[str, datetime] = {"revoked_at": now}
    if used:
        values["last_used_at"] = now
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.id == token_id, RefreshToken.revoked_at.is_(None))
        .values(**values)
    )


async def revoke_refresh_token_by_hash(
    session: AsyncSession, token_hash: str, now: datetime
) -> None:
    """Logout (AUTH-4): one UPDATE; an unknown or already revoked token changes nothing."""
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == token_hash, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )


async def revoke_all_refresh_tokens(session: AsyncSession, user_id: int, now: datetime) -> int:
    """Log the user out everywhere (token reuse, password reset, deactivation).
    Returns how many sessions were still active."""
    result = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return result.rowcount


# ── Password-reset tokens ─────────────────────────────────────────────────────


async def create_reset_token(
    session: AsyncSession, *, user_id: int, token_hash: str, expires_at: datetime
) -> PasswordResetToken:
    token = PasswordResetToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
    session.add(token)
    await session.flush()
    return token


async def get_reset_token_for_update(
    session: AsyncSession, token_hash: str
) -> PasswordResetToken | None:
    return await session.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == token_hash)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def mark_reset_token_used(session: AsyncSession, token_id: int, now: datetime) -> None:
    await session.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.id == token_id, PasswordResetToken.used_at.is_(None))
        .values(used_at=now)
    )


async def invalidate_reset_tokens(session: AsyncSession, user_id: int, now: datetime) -> int:
    """Before a new link is sent (AUTH-5), older unused links stop working.

    They are marked as used (``used_at = now``) rather than deleted, so the
    history of reset requests is kept. Returns how many were still unused.
    """
    result = await session.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.user_id == user_id, PasswordResetToken.used_at.is_(None))
        .values(used_at=now)
    )
    return result.rowcount
