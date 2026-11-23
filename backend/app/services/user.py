"""Users: your own profile and user administration (docs/api.md, USR-1 to USR-4)."""

import secrets
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.core.permissions import Action, Feature
from app.core.security import hash_password
from app.db.errors import constraint_name
from app.models import User
from app.repositories import organization as organization_repository
from app.repositories import user as user_repository
from app.schemas.common import Page, RoleRef, SortOrder
from app.schemas.user import (
    ActionFlags,
    CreateUserRequest,
    MeOrganization,
    MeResponse,
    OrgPermissions,
    UpdateMeRequest,
    UserOut,
    UserSort,
)
from app.services.auth import create_password_link
from app.services.authorization import Permissions
from app.services.email import EmailSender
from app.utils.pagination import PageParams

# The org-level features shown in /users/me (the four the navigation needs).
_ME_FEATURES = (Feature.ORGANIZATION, Feature.USERS, Feature.ROLES, Feature.PROJECTS)


# ── USR-1 and USR-2: me ───────────────────────────────────────────────────────


async def get_me(session: AsyncSession, user: User, permissions: Permissions) -> MeResponse:
    """``permissions`` are the user's org-wide permissions, already loaded for
    this request (app/api/deps.py), so no extra query is needed for them."""
    organization = await organization_repository.get_by_id(session, user.organization_id)
    flags = {
        feature.value: ActionFlags(
            **{action.value: action in permissions.org_grants[feature] for action in Action}
        )
        for feature in _ME_FEATURES
    }
    return MeResponse(
        id=user.id,
        name=user.name,
        email=user.email,
        organization=MeOrganization.model_validate(organization),
        org_role=permissions.org_role,
        org_permissions=OrgPermissions(**flags),
    )


async def update_me(
    session: AsyncSession, user: User, permissions: Permissions, data: UpdateMeRequest
) -> MeResponse:
    if data.name is not None:
        await user_repository.set_name(session, user.id, data.name)
        await session.commit()
    return await get_me(session, user, permissions)


# ── USR-3 and USR-4: administration ──────────────────────────────────────────


async def list_users(
    session: AsyncSession,
    organization_id: int,
    *,
    q: str | None,
    is_active: bool | None,
    sort: UserSort,
    order: SortOrder,
    page: PageParams,
) -> Page[UserOut]:
    rows, pagination = await user_repository.list_in_organization(
        session,
        organization_id,
        q=q,
        is_active=is_active,
        sort=sort,
        order=order,
        page=page,
    )
    items = [_user_out(user, role_id, role_name) for user, role_id, role_name in rows]
    return Page[UserOut](items=items, pagination=pagination)


async def create_user(
    session: AsyncSession,
    organization_id: int,
    data: CreateUserRequest,
    email_sender: EmailSender,
) -> UserOut:
    """Add a colleague to the caller's organization. They get no role (an admin
    assigns one, USR-6) and an email with a link to set their own password."""
    now = datetime.now(UTC)
    # A random password nobody knows, so the account can't be used until the
    # new user sets their own through the link. Hashed like any real password.
    password_hash = await hash_password(secrets.token_urlsafe(32))

    try:
        user = await user_repository.create(
            session,
            organization_id=organization_id,
            name=data.name,
            email=data.email,
            password_hash=password_hash,
        )
    except IntegrityError as exc:
        await session.rollback()
        if constraint_name(exc) == "uq_users_email":
            raise ConflictError(
                "An account with this email already exists.", field="email"
            ) from exc
        raise

    link = await create_password_link(session, user.id, now)
    await session.commit()  # the link must work before it's sent
    await email_sender.send_invitation(to=user.email, link=link)
    return _user_out(user, None, None)


def _user_out(user: User, role_id: int | None, role_name: str | None) -> UserOut:
    return UserOut(
        id=user.id,
        name=user.name,
        email=user.email,
        is_active=user.is_active,
        org_role=RoleRef(id=role_id, name=role_name) if role_id is not None else None,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )
