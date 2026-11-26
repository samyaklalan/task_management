"""Request and response shapes for teams (docs/api.md, TEAM-1 to TEAM-8)."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from app.models.team import TEAM_NAME_MAX_LENGTH
from app.schemas.common import Description, Id, RequestModel, UserRef

TeamName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=TEAM_NAME_MAX_LENGTH),
]
TeamSort = Literal["name", "created_at"]
TeamMemberSort = Literal["name", "joined_at"]

MAX_USERS_PER_ADD = 50


# ── Requests ──────────────────────────────────────────────────────────────────


class CreateTeamRequest(RequestModel):
    """TEAM-2. Team names don't have to be unique (no constraint in the schema)."""

    name: TeamName
    description: Description | None = None


class UpdateTeamRequest(RequestModel):
    """TEAM-4. Send only the fields to change, plus ``expected_updated_at``."""

    name: TeamName | None = None
    description: Description | None = None
    expected_updated_at: AwareDatetime

    @field_validator("name")
    @classmethod
    def _not_null(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("may be left out, but not set to null")
        return value


class AddTeamMembersRequest(RequestModel):
    """TEAM-7. Repeated ids count once (first occurrence keeps its place)."""

    user_ids: Annotated[list[Id], Field(min_length=1, max_length=MAX_USERS_PER_ADD)]

    @field_validator("user_ids")
    @classmethod
    def _unique(cls, user_ids: list[int]) -> list[int]:
        return list(dict.fromkeys(user_ids))


# ── Responses ─────────────────────────────────────────────────────────────────


class TeamOut(BaseModel):
    """TEAM-1 item; also the answer of TEAM-2, TEAM-3 and TEAM-4."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    member_count: int
    created_at: datetime
    updated_at: datetime


class TeamMemberOut(BaseModel):
    """TEAM-6 item. ``joined_at`` is team_members.created_at."""

    user: UserRef
    joined_at: datetime


class AddTeamMembersResponse(BaseModel):
    """TEAM-7: ``skipped`` were already in the team (a safe retry)."""

    added: list[int]
    skipped: list[int]
