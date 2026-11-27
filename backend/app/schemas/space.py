"""Request and response shapes for spaces (docs/api.md, SPC-1 to SPC-5)."""

from datetime import datetime
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, field_validator

from app.models.space import SPACE_NAME_MAX_LENGTH
from app.schemas.common import Description, Id, RequestModel

SpaceName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=SPACE_NAME_MAX_LENGTH),
]

SPACES_PAGE_SIZE = 50  # SPC-1's default page size (max stays 100)
MAX_SPACES_IN_ORDER = 500


class CreateSpaceRequest(RequestModel):
    """SPC-2. Added at the end of the sidebar."""

    name: SpaceName
    description: Description | None = None


class UpdateSpaceRequest(RequestModel):
    """SPC-4. Send only the fields to change, plus ``expected_updated_at``."""

    name: SpaceName | None = None
    description: Description | None = None
    expected_updated_at: AwareDatetime

    @field_validator("name")
    @classmethod
    def _not_null(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("may be left out, but not set to null")
        return value


class ReorderSpacesRequest(RequestModel):
    """SPC-5: every space of the project, once, in the new order. Duplicates and
    missing or foreign ids are a 422 ``invalid_reference`` (checked against the
    locked rows by the service), not removed silently."""

    space_ids: Annotated[list[Id], Field(min_length=1, max_length=MAX_SPACES_IN_ORDER)]


class SpaceOut(BaseModel):
    """SPC-1 item; also the answer of SPC-2, SPC-3 and SPC-4."""

    id: int
    name: str
    description: str | None
    position: int
    task_count: int
    created_at: datetime
    updated_at: datetime
