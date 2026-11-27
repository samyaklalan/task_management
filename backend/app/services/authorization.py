"""Who may do what: a user's effective permissions (docs/api.md, section 5.2, A-5).

A user can have one role for the whole organization (``user_roles.project_id``
NULL) and one role per project. The rules:

1. ``organization``, ``users``, ``roles`` and ``projects.create`` count only
   from the org-wide role (``is_org_scoped`` in app/core/permissions.py).
2. Everything else is allowed if EITHER role allows it (OR).
3. With no org-wide role and no role in the project, the project "doesn't
   exist" for the user (404, checked by the project lookup, M7).

``load_permissions`` reads both roles and their permission rows in ONE query;
the result answers every check in the request.
"""

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Action, Feature, is_org_scoped
from app.models import Role, RolePermission, UserRole
from app.schemas.common import RoleRef

Grants = dict[Feature, set[Action]]


# Two kinds of role (both rows in user_roles):
#   - Org-level role:     project_id IS NULL. Applies to the whole organization and
#                         every project. Only this role can grant organization, users,
#                         roles and projects.create.
#   - Project-level role: project_id = <a project's id>. Applies only to that project.
#
# Permissions is a dataclass. The constructor sets org_role and project_role to None
# if nothing is passed. org_grants and project_grants start as empty dicts: the key
# is a feature (e.g. tasks) and the value is a set of actions (e.g. {create, edit,
# delete}). They are defaultdict(set), so a missing feature gives an empty set
# (not allowed) instead of a KeyError.
@dataclass
class Permissions:
    org_role: RoleRef | None = None  # the user's org-level role, if any
    project_role: RoleRef | None = None  # their project-level role in this project, if any
    org_grants: Grants = field(default_factory=lambda: defaultdict(set))
    project_grants: Grants = field(default_factory=lambda: defaultdict(set))

    # allows(feature, action):
    #   1. If the action is in org_grants[feature], return True (the org-level role
    #      allows it).
    #   2. If is_org_scoped(feature, action) is True, return False. This applies only
    #      to organization, users, roles and projects.create, not tasks.
    #   3. Otherwise, return whether the action is in project_grants[feature] (the
    #      project-level role).
    def allows(self, feature: Feature, action: Action) -> bool:
        if action in self.org_grants[feature]:
            return True
        if is_org_scoped(feature, action):
            return False  # rule 1: a project role can't grant this
        return action in self.project_grants[feature]  # rule 2

    # can_see_project is a @property, so you call it without (): perms.can_see_project.
    # It still needs a Permissions object. It's True if the user has an org-level role
    # or a project-level role. If neither, the project returns 404.
    @property
    def can_see_project(self) -> bool:
        """Rule 3: any org-wide role or a role in this project."""
        return self.org_role is not None or self.project_role is not None

    # allow only check the feature and permissions or tings are allowed or not based
    # on pthe project_role and orhganization role


# load_permissions fills org_grants and project_grants from one database query.
#
# 1. It takes the logged-in user's user_id and the project_id being checked.
# 2. session.execute runs one query:
#    - It starts from user_roles and joins roles (Role.id == UserRole.role_id) to get
#      the role's id and name (e.g. 4, "Team Lead").
#    - It left joins role_permissions (RolePermission.role_id == Role.id) to get each
#      feature and its CRUD flags (e.g. tasks: view, create, edit).
#    - WHERE user_id matches AND (project_id IS NULL (org-level role) OR
#      project_id == project_id (project-level role)).
# 3. The loop puts each row in the right place. Each role is kept as a RoleRef
#    (app/schemas/common.py), which holds only the role's id and name, e.g.
#    RoleRef(id=4, name="Team Lead"):
#    - project_id NULL -> org_role and org_grants
#    - project_id set  -> project_role and project_grants
# 4. It returns a Permissions object, which allows() and can_see_project then use
#    to answer permission checks.
async def load_permissions(
    session: AsyncSession, user_id: int, project_id: int | None = None
) -> Permissions:
    """The user's org-wide role, plus their role in ``project_id`` if given."""
    rows = await session.execute(
        select(
            UserRole.project_id,
            Role.id,
            Role.name,
            RolePermission.feature,
            RolePermission.view_perm,
            RolePermission.create_perm,
            RolePermission.edit_perm,
            RolePermission.delete_perm,
        )
        .join(Role, Role.id == UserRole.role_id)
        # LEFT JOIN: a role with no permission rows is still a role (rule 3).
        .outerjoin(RolePermission, RolePermission.role_id == Role.id)
        .where(
            UserRole.user_id == user_id,
            or_(UserRole.project_id.is_(None), UserRole.project_id == project_id),
        )
    )

    permissions = Permissions()
    for row in rows:
        is_org_wide = row.project_id is None
        role = RoleRef(id=row.id, name=row.name)  # only id and name, e.g. 4, "Team Lead"
        if is_org_wide:
            permissions.org_role = role
        else:
            permissions.project_role = role
        if row.feature is None:
            continue
        try:
            feature = Feature(row.feature)
        except ValueError:
            continue  # a feature this version doesn't know: grants nothing
        grants = permissions.org_grants if is_org_wide else permissions.project_grants
        flags = (row.view_perm, row.create_perm, row.edit_perm, row.delete_perm)
        grants[feature].update(action for action, on in zip(Action, flags, strict=True) if on)
    return permissions


# How the loop fills the grants (the last 3 lines of the loop):
#   1. grants = org_grants if the row's project_id is NULL, else project_grants.
#   2. flags = the row's 4 flags (view, create, edit, delete), e.g. (True, True, True, False).
#   3. zip(Action, flags) pairs each action with its flag; "if on" keeps only the True ones,
#      and .update() adds them to the set, e.g. grants[TASKS] = {VIEW, CREATE, EDIT}.
#      strict=True raises an error if Action and flags ever have different lengths.
# In short: pick org or project grants, then add the actions whose flag is True
# for this feature.
