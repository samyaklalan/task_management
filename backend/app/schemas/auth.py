"""Request and response shapes for sign-up, login and tokens (docs/api.md, AUTH-1 to AUTH-6)."""

import re
import secrets
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.models.organization import (
    ORGANIZATION_NAME_MAX_LENGTH,
    ORGANIZATION_SLUG_MAX_LENGTH,
    ORGANIZATION_SLUG_PATTERN,
)
from app.schemas.common import RequestModel
from app.schemas.user import Email, PersonName, UserSummary

# A-12: 8-128 characters, no rules about symbols or digits. Not trimmed:
# spaces at the start or end are part of the password. The 128 limit stops
# someone from sending a huge string to make hashing slow.
Password = Annotated[str, Field(min_length=8, max_length=128)]

# Login checks only the upper limit: an old password that doesn't meet today's
# minimum must still be able to log in (and a wrong one simply fails).
LoginPassword = Annotated[str, Field(min_length=1, max_length=128)]

# Refresh and reset tokens are 43 characters. The limit only keeps a garbage
# value from being hashed and looked up.
OpaqueTokenStr = Annotated[str, Field(min_length=1, max_length=128)]

OrganizationName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=ORGANIZATION_NAME_MAX_LENGTH),
]
OrganizationSlug = Annotated[
    str,
    Field(
        min_length=1,
        max_length=ORGANIZATION_SLUG_MAX_LENGTH,
        pattern=ORGANIZATION_SLUG_PATTERN,
        description="Lowercase letters and digits, words joined by single hyphens.",
    ),
]


def slugify(text: str) -> str:
    """``"Acme Corp"`` → ``"acme-corp"``, ``"Café Déjà Vu!"`` → ``"cafe-deja-vu"``.

    Accents are dropped (é → e); other characters become hyphens; the result is
    cut to the column's 60 characters. Returns ``""`` if nothing usable is left
    (e.g. a name written only in non-Latin script).
    """
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:ORGANIZATION_SLUG_MAX_LENGTH].rstrip("-")


# ── Requests ──────────────────────────────────────────────────────────────────


class RegisterRequest(RequestModel):
    """AUTH-1. ``organization_slug`` is optional; it's made from the name if left out."""

    organization_name: OrganizationName
    organization_slug: OrganizationSlug | None = None
    name: PersonName
    email: Email
    password: Password

    @model_validator(mode="after")
    def _fill_in_slug(self) -> "RegisterRequest":
        if self.organization_slug is None:
            # Fallback for names with no Latin letters or digits, e.g. "株式会社".
            self.organization_slug = slugify(self.organization_name) or (
                f"org-{secrets.token_hex(4)}"
            )
        return self


class LoginRequest(RequestModel):
    """AUTH-2."""

    email: Email
    password: LoginPassword


class RefreshRequest(RequestModel):
    """AUTH-3 and AUTH-4."""

    refresh_token: OpaqueTokenStr


class PasswordResetRequest(RequestModel):
    """AUTH-5."""

    email: Email


class PasswordResetConfirm(RequestModel):
    """AUTH-6."""

    token: OpaqueTokenStr
    new_password: Password


# ── Responses ─────────────────────────────────────────────────────────────────


class TokenResponse(BaseModel):
    """AUTH-3. Lifetimes are in seconds."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  (OAuth2 token type, not a secret)
    expires_in: int
    refresh_token: str
    refresh_expires_in: int


class LoginResponse(TokenResponse):
    """AUTH-1 and AUTH-2: the tokens plus who logged in."""

    user: UserSummary


class MessageResponse(BaseModel):
    """AUTH-5."""

    message: str
