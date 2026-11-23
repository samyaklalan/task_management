"""Database access for users.

Repositories only read and write rows: no business rules, no HTTP errors, and
they never commit (the service decides when the transaction ends). A duplicate
email surfaces as ``IntegrityError`` on ``uq_users_email``; the service turns it
into ``409 conflict`` (insert and catch, never check-then-insert).

``password_hash`` is deferred on the model: only ``get_by_email_with_password``
loads it, for login. Everything else returns users without it.
"""

from typing import Any

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.models import Role, User, UserRole
from app.schemas.common import Pagination, SortOrder
from app.schemas.user import UserSort
from app.utils.pagination import PageParams, paginate
from app.utils.search import LIKE_ESCAPE, contains


async def get_by_id(session: AsyncSession, user_id: int) -> User | None:
    """Primary-key lookup; used on every authenticated request (api.md section 2.1)."""
    return await session.get(User, user_id)


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    """``email`` must already be lowercase (the schemas do that)."""
    return await session.scalar(select(User).where(User.email == email))


# Used by login: gets the user by email, also loading password_hash, so the
# service can check whether the password is correct.
async def get_by_email_with_password(session: AsyncSession, email: str) -> User | None:
    """For login (AUTH-2): the same lookup, also loading ``password_hash``."""
    return await session.scalar(
        select(User).where(User.email == email).options(undefer(User.password_hash))
    )


async def create(
    session: AsyncSession,
    *,
    organization_id: int,
    name: str,
    email: str,
    password_hash: str,
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


async def set_name(session: AsyncSession, user_id: int, name: str) -> None:
    """USR-2: change the user's own name. One UPDATE, without loading the user.

    A bulk ``update()`` doesn't run the model's ``onupdate``, so ``updated_at``
    is set here (app/db/base.py, TimestampMixin).
    """
    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(name=name, updated_at=func.statement_timestamp())
    )


_SORT_COLUMNS = {
    "name": func.lower(User.name),  # "anil" and "Anil" sort together
    "email": User.email,
    "created_at": User.created_at,
}


async def list_in_organization(
    session: AsyncSession,
    organization_id: int,
    *,
    q: str | None,
    is_active: bool | None,
    sort: UserSort,
    order: SortOrder,
    page: PageParams,
) -> tuple[list[Any], Pagination]:
    """USR-3: one page of the organization's users, each with their org-wide
    role. Items are rows ``(user, role_id, role_name)``; the role is ``None``
    for users without one.

    Filters and search apply to the whole list before paging (section 4.3);
    ``id`` breaks ties so rows never jump between pages.
    """
    statement: Select[Any] = (
        select(User, Role.id.label("role_id"), Role.name.label("role_name"))
        .outerjoin(UserRole, and_(UserRole.user_id == User.id, UserRole.project_id.is_(None)))
        .outerjoin(Role, Role.id == UserRole.role_id)
        .where(User.organization_id == organization_id)
    )
    if is_active is not None:
        statement = statement.where(User.is_active.is_(is_active))
    if q is not None:
        pattern = contains(q)
        statement = statement.where(
            or_(
                User.name.ilike(pattern, escape=LIKE_ESCAPE),
                User.email.ilike(pattern, escape=LIKE_ESCAPE),
            )
        )
    column = _SORT_COLUMNS[sort]
    if order == "desc":
        statement = statement.order_by(column.desc(), User.id.desc())
    else:
        statement = statement.order_by(column.asc(), User.id.asc())

    return await paginate(session, statement, page, scalars=False)
