"""Request and response shapes for the organization (docs/api.md, ORG-1 and ORG-2)."""

from datetime import datetime
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    StringConstraints,
    field_validator,
)

from app.models.organization import ORGANIZATION_NAME_MAX_LENGTH
from app.schemas.common import Description, RequestModel

OrganizationName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=ORGANIZATION_NAME_MAX_LENGTH
    ),
]


class OrganizationOut(BaseModel):
    """ORG-1 and ORG-2. The internal id is not shown (the organization is
    always "the caller's own")."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    slug: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class UpdateOrganizationRequest(RequestModel):
    """ORG-2. Send only the fields to change. ``slug`` can't be changed (A-8):
    sending it is a 422 (unknown field)."""

    name: OrganizationName | None = None
    description: Description | None = None
    expected_updated_at: AwareDatetime

    @field_validator("name")
    @classmethod
    def _name_not_null(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("may be left out, but not set to null")
        return value


# Summary:
# - OrganizationName, Description: shared types (trimmed); imported by other files.
# - OrganizationOut: response for ORG-1/ORG-2; the internal id is not shown.
# - UpdateOrganizationRequest (ORG-2, partial update):
#     name: leave out = keep; null = 422.
#       The name validator runs only when name is sent (Pydantic doesn't
#       validate defaults), so leaving it out is fine.
#     description: leave out = keep; blank or null = clear.
#       Text is trimmed first, so "" or "   " becomes "" and then None.
#     expected_updated_at: required, for the 409 stale-update check.
#       AwareDatetime: must include a time zone, e.g. "2026-11-23T10:00:00Z".
#     slug: can't be changed (A-8); sending it = 422 (unknown field), because
#       RequestModel has extra="forbid".
# - The service will use model_dump(exclude_unset=True): only sent fields are updated.
# - Open point: the 2000-character description limit is not in docs/api.md or
#   docs/database.md (the column is Text, no limit). It needs approval, and the
#   constant should then move to app/models/organization.py (AGENTS.md 6 and 7).
