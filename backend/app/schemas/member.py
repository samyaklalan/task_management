"""Request and response shapes for project members (docs/api.md, MEM-1 to MEM-4)."""

from typing import Literal

from pydantic import BaseModel

from app.schemas.common import Id, RequestModel, RoleRef, UserRef

# "organization": an org-wide role gives access (managed with USR-6 / USR-7).
# "project": a role in this project (managed here, MEM-2 to MEM-4).
MemberScope = Literal["organization", "project"]


class MemberOut(BaseModel):
    user: UserRef
    role: RoleRef
    scope: MemberScope


class AddMemberRequest(RequestModel):
    """MEM-2."""

    user_id: Id
    role_id: Id


class ChangeMemberRoleRequest(RequestModel):
    """MEM-3."""

    role_id: Id
