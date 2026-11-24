"""Request and response shapes for roles (docs/api.md, ROLE-1 to ROLE-4)."""

from datetime import datetime
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from app.core.permissions import Feature
from app.models.role import ROLE_NAME_MAX_LENGTH
from app.schemas.common import RequestModel

RoleName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=ROLE_NAME_MAX_LENGTH),
]


class FeaturePermissions(BaseModel):
    """One feature's four switches. In requests, a missing switch means false;
    an unknown ``feature`` or switch (e.g. "approve") is a 422."""

    model_config = ConfigDict(extra="forbid")

    feature: Feature
    view: bool = False
    create: bool = False
    edit: bool = False
    delete: bool = False


PermissionList = Annotated[list[FeaturePermissions], Field(max_length=len(Feature))]


def _no_duplicate_features(
    permissions: list[FeaturePermissions],
) -> list[FeaturePermissions]:
    features = [item.feature for item in permissions]
    duplicates = sorted({feature.value for feature in features if features.count(feature) > 1})
    if duplicates:
        raise ValueError(f"each feature may appear only once: {', '.join(duplicates)}")
    return permissions


class RoleOut(BaseModel):
    """ROLE-1. ``permissions`` always lists all ten features, in a fixed order."""

    id: int
    name: str
    assigned_count: int
    permissions: list[FeaturePermissions]
    created_at: datetime
    updated_at: datetime


class RoleListResponse(BaseModel):
    """ROLE-1: not paginated (an organization has a handful of roles)."""

    items: list[RoleOut]


class CreateRoleRequest(RequestModel):
    """ROLE-2. Features not listed get no permissions."""

    name: RoleName
    permissions: PermissionList = []

    _unique = field_validator("permissions")(_no_duplicate_features)


class UpdateRoleRequest(RequestModel):
    """ROLE-3. If ``permissions`` is sent, it becomes the COMPLETE list:
    features not listed are switched off."""

    name: RoleName | None = None
    permissions: PermissionList | None = None
    expected_updated_at: AwareDatetime

    @field_validator("name", "permissions")
    @classmethod
    def _not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("may be left out, but not set to null")
        return value

    @field_validator("permissions")
    @classmethod
    def _unique(cls, value: list[FeaturePermissions]) -> list[FeaturePermissions]:
        return _no_duplicate_features(value)
