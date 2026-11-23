"""Sign-up, login and password reset (docs/api.md, AUTH-1, AUTH-2 and AUTH-5).

Services hold the business rules and decide when the transaction ends
(``commit``). They raise the errors of app/core/exceptions.py; the routers stay
thin.
"""

from datetime import UTC, datetime
from urllib.parse import urlencode

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ConflictError, InvalidCredentialsError
from app.core.permissions import ADMIN_ROLE_NAME, admin_permission_rows
from app.core.security import (
    create_access_token,
    generate_token,
    hash_password,
    password_needs_rehash,
    verify_dummy_password,
    verify_password,
)
from app.db.errors import constraint_name
from app.models import User
from app.repositories import organization as organization_repository
from app.repositories import role as role_repository
from app.repositories import token as token_repository
from app.repositories import user as user_repository
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    MessageResponse,
    PasswordResetRequest,
    RegisterRequest,
    TokenResponse,
)
from app.schemas.user import UserSummary
from app.services.email import EmailSender

# AUTH-5: the same answer whether or not the email has an account (docs/api.md).
RESET_REQUESTED_MESSAGE = "If an account exists for this email, a reset link has been sent."

# Which unique constraint means which field is taken (AUTH-1: 409 conflict).
_REGISTER_CONFLICTS = {
    "uq_organizations_slug": (
        "organization_slug",
        "This organization URL is already taken.",
    ),
    "uq_users_email": ("email", "An account with this email already exists."),
}


async def register(session: AsyncSession, data: RegisterRequest) -> LoginResponse:
    """Create the organization, its Admin role, the first user and their
    org-wide role in one transaction, then log the user in."""
    now = datetime.now(UTC)
    # Hash before touching the database: the ~0.1 s of hashing doesn't keep a
    # transaction (and a pooled connection) waiting.
    password_hash = await hash_password(data.password)

    try:
        organization = await organization_repository.create(
            session, name=data.organization_name, slug=data.organization_slug
        )
        role = await role_repository.create(
            session, organization_id=organization.id, name=ADMIN_ROLE_NAME
        )
        await role_repository.add_permissions(session, role.id, admin_permission_rows())
        user = await user_repository.create(
            session,
            organization_id=organization.id,
            name=data.name,
            email=data.email,
            password_hash=password_hash,
        )
        await role_repository.assign_org_role(session, user_id=user.id, role_id=role.id)
    except IntegrityError as exc:
        await session.rollback()  # nothing of the half-done sign-up is kept
        conflict = _REGISTER_CONFLICTS.get(constraint_name(exc))
        if conflict is None:
            raise
        field, message = conflict
        raise ConflictError(message, field=field) from exc

    tokens = await _issue_tokens(session, user, now)
    await session.commit()
    return LoginResponse(**tokens.model_dump(), user=UserSummary.model_validate(user))


async def login(session: AsyncSession, data: LoginRequest) -> LoginResponse:
    """Check email and password. Every failure gives the same answer in about
    the same time, so nobody can find out which emails have accounts."""
    now = datetime.now(UTC)
    user = await user_repository.get_by_email_with_password(session, data.email)

    if user is None:
        await verify_dummy_password(data.password)  # same work as a real check
        raise InvalidCredentialsError()
    password_ok = await verify_password(data.password, user.password_hash)
    if not password_ok or not user.is_active:  # inactive: same answer as a wrong password
        raise InvalidCredentialsError()

    if password_needs_rehash(user.password_hash):
        # Hash settings were raised since this password was saved: save a new hash.
        new_hash = await hash_password(data.password)
        await user_repository.set_password_hash(session, user.id, new_hash)

    tokens = await _issue_tokens(session, user, now)
    await session.commit()
    return LoginResponse(**tokens.model_dump(), user=UserSummary.model_validate(user))


async def request_password_reset(
    session: AsyncSession, data: PasswordResetRequest, email_sender: EmailSender
) -> MessageResponse:
    """Send a reset link to an active user. The answer is the same for any email,
    so this can't be used to find out who has an account."""
    now = datetime.now(UTC)
    user = await user_repository.get_by_email(session, data.email)

    if user is not None and user.is_active:
        link = await create_password_link(session, user.id, now)
        await session.commit()  # the link must work before it's sent
        await email_sender.send_password_reset(to=user.email, link=link)

    return MessageResponse(message=RESET_REQUESTED_MESSAGE)


async def create_password_link(session: AsyncSession, user_id: int, now: datetime) -> str:
    """A new single-use link to set a password; older unused links stop working.

    Used for "forgot password" (AUTH-5) and for new users (USR-4). The caller
    commits, then sends the link.
    """
    settings = get_settings()
    await token_repository.invalidate_reset_tokens(session, user_id, now)
    token, token_hash = generate_token()
    await token_repository.create_reset_token(
        session,
        user_id=user_id,
        token_hash=token_hash,
        expires_at=now + settings.password_reset_token_ttl,
    )
    return f"{settings.frontend_base_url}/reset-password?{urlencode({'token': token})}"


async def _issue_tokens(session: AsyncSession, user: User, now: datetime) -> TokenResponse:
    """A new access token and a new refresh token (a new session for this device)."""
    settings = get_settings()
    refresh_token, refresh_hash = generate_token()
    await token_repository.create_refresh_token(
        session,
        user_id=user.id,
        token_hash=refresh_hash,
        expires_at=now + settings.refresh_token_ttl,
    )
    return TokenResponse(
        access_token=create_access_token(user.id, user.organization_id),
        expires_in=int(settings.access_token_ttl.total_seconds()),
        refresh_token=refresh_token,
        refresh_expires_in=int(settings.refresh_token_ttl.total_seconds()),
    )
