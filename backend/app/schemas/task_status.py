"""Request and response shapes for task statuses (docs/api.md, STS-1 to STS-5)."""

from datetime import datetime
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, field_validator

from app.models.task_status import (
    COLOR_PATTERN,
    TASK_STATUS_NAME_MAX_LENGTH,
    TaskStatusCategory,
)
from app.schemas.common import Id, RequestModel

StatusName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=TASK_STATUS_NAME_MAX_LENGTH),
]
# "#3b82f6" is accepted and stored as "#3B82F6" (the pattern allows both cases).
Color = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=COLOR_PATTERN, to_upper=True)
]

MAX_STATUSES_IN_ORDER = 100


class CreateStatusRequest(RequestModel):
    """STS-2. Added at the end of the board. Names don't have to be unique."""

    name: StatusName
    color: Color
    category: TaskStatusCategory


class UpdateStatusRequest(RequestModel):
    """STS-3. Send only the fields to change, plus ``expected_updated_at``."""

    name: StatusName | None = None
    color: Color | None = None
    category: TaskStatusCategory | None = None
    expected_updated_at: AwareDatetime

    @field_validator("name", "color", "category")
    @classmethod
    def _not_null(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("may be left out, but not set to null")
        return value


class ReorderStatusesRequest(RequestModel):
    """STS-4: every status of the project, once, in the new order (checked by
    the service against the locked rows)."""

    status_ids: Annotated[list[Id], Field(min_length=1, max_length=MAX_STATUSES_IN_ORDER)]


class StatusOut(BaseModel):
    """STS-1 item; also the answer of STS-2 and STS-3."""

    id: int
    name: str
    color: str
    category: TaskStatusCategory
    position: int
    task_count: int
    updated_at: datetime


class StatusList(BaseModel):
    """STS-1 and STS-4: not paginated (a board has a handful of columns)."""

    items: list[StatusOut]
