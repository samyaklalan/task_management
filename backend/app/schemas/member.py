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


# Summary of this file, in my words
# ---------------------------------
# MemberOut: the response for one member (MEM-1 list, MEM-2 add, MEM-3 change role).
#   user:  UserRef -> {id, name}  (who; no email)
#   role:  RoleRef -> {id, name}  (which role)
#   scope: MemberScope
#     "project":      member because of a role in THIS project only (managed here,
#                     MEM-2 to MEM-4)
#     "organization": access because of their org-wide role, which covers every
#                     project (managed with USR-6 / USR-7, not here)
#
# AddMemberRequest (MEM-2): POST /projects/{public_id}/members
#   {"user_id": 7, "role_id": 4}
#   Adds the user to the PROJECT (not a team) with a project role: one user_roles row
#   (user_id=7, role_id=4, project_id=this project). Response scope = "project".
#
# ChangeMemberRoleRequest (MEM-3): only role_id in the body; WHICH member comes from
#   the URL (/projects/{public_id}/members/{user_id}).
#
# Both requests extend RequestModel: an unknown field -> 422.
# Id = the shared id type from app/schemas/common.py.
