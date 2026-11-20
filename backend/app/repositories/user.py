"""Database access for users.

Repositories only read and write rows: no business rules, no HTTP errors, and
they never commit (the service decides when the transaction ends). A duplicate
email surfaces as ``IntegrityError`` on ``uq_users_email``; the service turns it
into ``409 conflict`` (insert and catch, never check-then-insert).

``password_hash`` is deferred on the model: only ``get_by_email_with_password``
loads it, for login. Everything else returns users without it.
"""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.models import User


async def get_by_id(session: AsyncSession, user_id: int) -> User | None:
    """Primary-key lookup; used on every authenticated request (api.md section 2.1)."""
    return await session.get(User, user_id)


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    """``email`` must already be lowercase (the schemas do that)."""
    return await session.scalar(select(User).where(User.email == email))


async def get_by_email_with_password(session: AsyncSession, email: str) -> User | None:
    """For login (AUTH-2): the same lookup, also loading ``password_hash``."""
    return await session.scalar(
        select(User).where(User.email == email).options(undefer(User.password_hash))
    )


async def create(
    session: AsyncSession, *, organization_id: int, name: str, email: str, password_hash: str
) -> User:
    user = User(
        organization_id=organization_id,
        name=name,
        email=email,
        password_hash=password_hash,
    )
    session.add(user)
    await session.flush()  # raises IntegrityError if the email is taken
    return user


async def set_password_hash(session: AsyncSession, user_id: int, password_hash: str) -> None:
    """New password (AUTH-6) or a re-hash with newer settings after login.

    One UPDATE, without loading the user; ``updated_at`` changes too.
    """
    await session.execute(
        update(User).where(User.id == user_id).values(password_hash=password_hash)
    )
