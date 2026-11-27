"""Request and response shapes for users (docs/api.md, USR-1 to USR-7).

Also defines the field types every user-related schema shares:
``PersonName`` (trimmed, 1-150 characters) and ``Email`` (valid, lowercased,
at most 254 characters). auth.py uses them too.

``password_hash`` never appears in any response: no response model here has
that field, so it can't be returned even by mistake.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
)

from app.models.user import USER_EMAIL_MAX_LENGTH, USER_NAME_MAX_LENGTH
from app.schemas.common import RequestModel, RoleRef

# ── Shared field types ────────────────────────────────────────────────────────

PersonName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=USER_NAME_MAX_LENGTH),
]

# Emails are stored lowercase (CHECK ck_users_email_lowercase), so "Asha@Acme.com"
# and "asha@acme.com" are the same account.
Email = Annotated[
    EmailStr,
    Field(max_length=USER_EMAIL_MAX_LENGTH),
    AfterValidator(str.lower),
]

UserSort = Literal["name", "email", "created_at"]


def _reject_null(value: object) -> object:
    """For optional PATCH fields: leaving a field out keeps it, but ``null`` is
    an error when the column can't be empty (section 1, "Partial updates")."""
    if value is None:
        raise ValueError("may be left out, but not set to null")
    return value


# ── Responses ─────────────────────────────────────────────────────────────────


# anyone who logs in (themselves)
class UserSummary(BaseModel):
    """The ``user`` object in the login response (AUTH-2)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str


# Yes. MeOrganization describes the organization
#  the logged-in user belongs to. I
class MeOrganization(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    slug: str


class ActionFlags(BaseModel):
    view: bool = False
    create: bool = False
    edit: bool = False
    delete: bool = False


# rgPermissions()
# organization: view=False create=False edit=False delete=False
# users:        view=False create=False edit=False delete=False
# roles:        view=False create=False edit=False delete=False
# projects:     view=False create=False edit=False delete=False


class OrgPermissions(BaseModel):
    """The four organization-level features (section 5.1). All false when the
    user has no org-wide role."""

    organization: ActionFlags = ActionFlags()
    users: ActionFlags = ActionFlags()
    roles: ActionFlags = ActionFlags()
    projects: ActionFlags = ActionFlags()


# the logged-in user (themselves)
class MeResponse(BaseModel):
    """USR-1 and USR-2."""

    id: int
    name: str
    email: str
    organization: MeOrganization
    org_role: RoleRef | None
    org_permissions: OrgPermissions


# MeResponse is used by the routes where the logged-
# in user reads or changes their own profile. Its docstring says USR-1 and USR-2:


# Route	Method	What it does	Request	Response
# USR-1 /me	GET	"Who am I?": fetch my own profile
# none	MeResponse
##SR-2 /me	PATCH	update my own display name
# UpdateMeRequest (name only)	MeResponse
class UserOut(BaseModel):
    """One user in the administration list (USR-3, USR-4, USR-5)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    is_active: bool
    org_role: RoleRef | None
    # roleref will come frokm the role table as we only need ifd and role not
    # view and delete or all crud permiision on diff feture
    created_at: datetime
    updated_at: datetime


class OrgRoleResponse(BaseModel):
    """USR-6."""

    user_id: int
    role: RoleRef
    # userid and roleid based on id role.name manager or led etc


# ── Requests ──────────────────────────────────────────────────────────────────


class UpdateMeRequest(RequestModel):
    """USR-2. Only the display name can be changed here."""

    name: PersonName | None = None
    # ti update the name means a route to uodate the user name whio is login but
    # name cannt be null
    _not_null = field_validator("name")(_reject_null)


class CreateUserRequest(RequestModel):
    """USR-4. The new user sets their own password through the emailed link."""

    name: PersonName
    email: Email


class UpdateUserRequest(RequestModel):
    """USR-5. ``expected_updated_at`` is the ``updated_at`` the admin saw; if the
    user changed since then, the update is refused with ``409 stale_update``."""

    name: PersonName | None = None
    is_active: bool | None = None
    expected_updated_at: AwareDatetime

    _not_null = field_validator("name", "is_active")(_reject_null)


class SetOrgRoleRequest(RequestModel):
    """USR-6."""

    # as it imported the request model from common.py and the role ref is also imported
    # from common.py
    # so by calllingf the route it will get tje user_id and role_id is set other fields
    #  which are send will be ignored as the request model has extra="forbid" in common.py
    # userole table also have project_id
    # but due to forbid we are only allowed to send the fields in the chema
    # no other fields will be accepted and if sent will be rejected with 422 error
    # forbid also checks the fields belong to the model or not if not it will
    #  throw 422 error
    role_id: int = Field(ge=1)
