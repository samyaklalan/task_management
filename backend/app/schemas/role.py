"""Request and response shapes for roles (docs/api.md, ROLE-1 to ROLE-4).

Validation only, no database logic.

- RoleName: string, spaces stripped, length 1..ROLE_NAME_MAX_LENGTH (from the model file).
- FeaturePermissions: one feature + 4 switches (view/create/edit/delete). A missing switch is
  false; an unknown feature or switch is a 422.
- PermissionList: list of FeaturePermissions, at most 10 (one per feature).
- _no_duplicate_features: the same feature listed twice is a 422.
- RoleOut: id, name, assigned_count, permissions (always all 10 features), created_at,
  updated_at.
- RoleListResponse: {"items": [RoleOut, ...]}, not paginated.
- CreateRoleRequest: name required; permissions default [] (features not listed are off);
  no duplicates.
- UpdateRoleRequest:
  - name / permissions are optional: left out = skipped, sent as null = 422.
  - permissions, if sent, REPLACES the whole list: features not listed are switched off.
  - expected_updated_at must include a timezone; if someone saved in between = 409
    stale_update.
- extra="forbid" (RequestModel, FeaturePermissions): an unknown field is a 422, never
  silently ignored.
"""

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
