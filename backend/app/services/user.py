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


# How get_me works:
#   1. permissions is NOT a request body (GET /me has none). It comes from the
#      OrgPermissionsDep dependency: load_permissions already read the user's
#      org-level role and what it allows from the database.
#   2. Load the organization by user.organization_id.
#   3. For each of the 4 features (_ME_FEATURES) and each action (view, create,
#      edit, delete): action in permissions.org_grants[feature] -> True if the
#      role allows it, False if not.
#   4. Build MeResponse: id, name, email, organization, org_role and these flags.
# MeResponse is ALWAYS returned; a user with no permissions just gets all False.
async def get_me(session: AsyncSession, user: User, permissions: Permissions) -> MeResponse:
    """``permissions`` are the user's org-wide permissions, already loaded for
    this request (app/api/deps.py), so no extra query is needed for them."""
    organization = await organization_repository.get_by_id(session, user.organization_id)
    # flags: a yes/no table, 4 features x 4 actions (view, create, edit, delete).
    #   - Outer loop: the 4 features in _ME_FEATURES.
    #   - Inner loop: action in permissions.org_grants[feature] -> True if the
    #     user's role allows it, False if not.
    #   - ActionFlags(**{...}): the 4 answers as an object (** = dict keys become
    #     field names); stored under feature.value, e.g. "users".
    #   - Example: a role with USERS: {VIEW, CREATE} gives "users": view=True,
    #     create=True, edit=False, delete=False. Features not in the role: all False.
    #   - Same as two normal for loops, written as one expression.
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
    # Each row is (user, role_id, role_name). _user_out runs once per row and the
    # results become items. Page[T] = items (a list of T, here UserOut) plus
    # pagination (page, page_size, total_items, ...) from paginate().
    items = [_user_out(user, role_id, role_name) for user, role_id, role_name in rows]
    return Page[UserOut](items=items, pagination=pagination)


# How create_user works (USR-4, POST /users: an admin adds a new user):
#   1. Get the time (now), used for the link's expiry.
#   2. Hash a RANDOM password (not the user's: the admin never sets it), so the
#      account can't be used until the user sets their own through the link.
#   3. user_repository.create(organization_id, name, email, password_hash).
#      The user gets no role yet (an admin assigns one later, USR-6).
#   4. IntegrityError -> rollback (on any); duplicate email -> 409, anything
#      else is raised as is. Insert and catch, never check-then-insert.
#   5. create_password_link(user.id, now): single-use token, expires in 30 minutes.
#   6. Commit FIRST, then send_invitation(user.email, link), so the link works
#      before it's sent.
#   7. Return one UserOut with org_role: null (status 201).
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
    # A single UserOut, not a Page. The new user has no role yet -> org_role: null.
    return _user_out(user, None, None)


# _user_out builds ONE UserOut (the response for one user) from a User plus their role:
#   - copies the safe fields: id, name, email, is_active, created_at, updated_at
#     (never password_hash);
#   - org_role: RoleRef(id, name), e.g. {"id": 1, "name": "Admin"}, or None (null)
#     when there is no role.
# Needed because the role isn't part of the User model, so
# UserOut.model_validate(user) couldn't fill org_role; the role comes from the
# query's Role.id and Role.name columns.
# Used by list_users (once per row -> Page[UserOut]) and create_user (one UserOut,
# role None). The _ means it's private to this file.
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
