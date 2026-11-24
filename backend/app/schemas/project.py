"""Request and response shapes for projects (docs/api.md, PRJ-1 to PRJ-6)."""

import re
from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.models.project import (
    PROJECT_NAME_MAX_LENGTH,
    TASK_PREFIX_PATTERN,
    ProjectStatus,
)
from app.schemas.common import Description, RequestModel
from app.schemas.user import ActionFlags

ProjectName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=PROJECT_NAME_MAX_LENGTH
    ),
]


def _check_prefix(prefix: str) -> str:
    if not re.fullmatch(TASK_PREFIX_PATTERN, prefix):
        raise ValueError(
            "2-10 letters or digits, starting with a letter (e.g. WEB, APP2)"
        )
    return prefix


# Uppercased first ("web" → "WEB"), then checked against P-5's pattern.
TaskPrefix = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True),
    AfterValidator(_check_prefix),
]

ProjectSort = Literal["name", "created_at", "updated_at", "end_date"]

# PRJ-4 may set these; "archived" only through PRJ-5 / PRJ-6.
EditableProjectStatus = Literal["active", "on_hold", "completed"]

DATE_RANGE_MESSAGE = "end_date can't be before start_date"


class _DateRange(BaseModel):
    """End date can't be before start date (same rule as ck_projects_date_range)."""

    @model_validator(mode="after")
    def _end_not_before_start(self) -> Self:
        start, end = getattr(self, "start_date", None), getattr(self, "end_date", None)
        if start is not None and end is not None and end < start:
            raise ValueError(DATE_RANGE_MESSAGE)
        return self


# ── Requests ──────────────────────────────────────────────────────────────────


class CreateProjectRequest(RequestModel, _DateRange):
    """PRJ-1. ``status`` always starts as ``active``."""

    name: ProjectName
    task_prefix: TaskPrefix
    description: Description | None = None
    start_date: date | None = None
    end_date: date | None = None


class UpdateProjectRequest(RequestModel, _DateRange):
    """PRJ-4. Send only the fields to change, plus ``expected_updated_at``.

    ``task_prefix`` can't be changed (A-8: every task key would change), so
    sending it is a 422 (unknown field), like ``slug`` in ORG-2. ``description``,
    ``start_date`` and ``end_date`` may be set to null to clear them.
    """

    name: ProjectName | None = None
    description: Description | None = None
    status: EditableProjectStatus | None = None
    start_date: date | None = None
    end_date: date | None = None
    expected_updated_at: AwareDatetime

    @field_validator("name", "status")
    @classmethod
    def _not_null(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("may be left out, but not set to null")
        return value


# ── Responses ─────────────────────────────────────────────────────────────────


class ProjectListItem(BaseModel):
    """PRJ-2. Deliberately without description and counts (not on the list screen)."""

    model_config = ConfigDict(from_attributes=True)

    public_id: str
    name: str
    status: ProjectStatus
    task_prefix: str
    start_date: date | None
    end_date: date | None
    updated_at: datetime


class TaskCounts(BaseModel):
    todo: int = 0
    in_progress: int = 0
    done: int = 0
    total: int = 0


class ProjectDetail(BaseModel):
    """PRJ-3 (also the answer of PRJ-1, PRJ-4, PRJ-5 and PRJ-6): everything the
    project page needs in one request."""

    public_id: str
    name: str
    status: ProjectStatus
    description: str | None
    task_prefix: str
    start_date: date | None
    end_date: date | None
    created_at: datetime
    updated_at: datetime
    task_counts: TaskCounts
    # The caller's effective permissions in this project, for the 7
    # project-level features (e.g. hide "New task" without tasks.create).
    my_permissions: dict[str, ActionFlags]


ProjectStatusFilter = Literal["active", "on_hold", "completed", "archived"]
