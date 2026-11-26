"""Request and response shapes for tasks (docs/api.md, TSK-1 to TSK-4, TSK-8)."""

from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.models.task import TASK_TITLE_MAX_LENGTH, TaskPriority
from app.models.task_status import TaskStatusCategory
from app.schemas.common import Id, RequestModel, UserRef

TaskTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=TASK_TITLE_MAX_LENGTH),
]
# Task descriptions are longer than the 2000 characters of project/team
# descriptions (bug reports, steps to reproduce); same limit as a comment body.
TASK_DESCRIPTION_MAX_LENGTH = 10_000
TaskDescription = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=TASK_DESCRIPTION_MAX_LENGTH),
]

TaskSort = Literal["created_at", "updated_at", "number", "priority", "end_date", "title"]

DATE_RANGE_MESSAGE = "end_date can't be before start_date"


class _DateRange(BaseModel):
    @model_validator(mode="after")
    def _end_not_before_start(self) -> Self:
        start, end = getattr(self, "start_date", None), getattr(self, "end_date", None)
        if start is not None and end is not None and end < start:
            raise ValueError(DATE_RANGE_MESSAGE)
        return self


def _blank_is_none(text: str | None) -> str | None:
    return text or None


# ── Requests ──────────────────────────────────────────────────────────────────


class CreateTaskRequest(RequestModel, _DateRange):
    """TSK-2. Only ``title`` is required; space and status default as the spec
    says, ``priority`` defaults to medium."""

    title: TaskTitle
    description: TaskDescription | None = None
    space_id: Id | None = None
    status_id: Id | None = None
    assignee_id: Id | None = None
    priority: TaskPriority = TaskPriority.MEDIUM
    start_date: date | None = None
    end_date: date | None = None

    _description = field_validator("description")(_blank_is_none)


class UpdateTaskRequest(RequestModel, _DateRange):
    """TSK-4: any subset of TSK-2's fields plus the required version check.

    ``assignee_id``, ``description`` and the dates may be null (clear them);
    title, space, status and priority may be left out but never null.
    """

    title: TaskTitle | None = None
    description: TaskDescription | None = None
    space_id: Id | None = None
    status_id: Id | None = None
    assignee_id: Id | None = None
    priority: TaskPriority | None = None
    start_date: date | None = None
    end_date: date | None = None
    expected_updated_at: AwareDatetime

    _description = field_validator("description")(_blank_is_none)

    @field_validator("title", "space_id", "status_id", "priority")
    @classmethod
    def _not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("may be left out, but not set to null")
        return value


# ── Responses ─────────────────────────────────────────────────────────────────


class SpaceRef(BaseModel):
    id: int
    name: str


class StatusRef(BaseModel):
    id: int
    name: str
    color: str
    category: TaskStatusCategory


class TaskListItem(BaseModel):
    """TSK-1 item. No description (TSK-3 has it)."""

    number: int
    key: str
    title: str
    priority: TaskPriority
    space: SpaceRef
    status: StatusRef
    assignee: UserRef | None
    start_date: date | None
    end_date: date | None
    created_at: datetime
    updated_at: datetime


class TaskDetail(TaskListItem):
    """TSK-3; also the answer of TSK-2 and TSK-4."""

    description: str | None
    created_by: UserRef
    comment_count: int
