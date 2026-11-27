"""Permissions: features, actions and the default Admin role (docs/api.md, section 5).

A permission is a feature (an area of the app, e.g. "tasks") plus an action
(view, create, edit or delete). The database stores one row per role and
feature in ``role_permissions``, with one true/false column per action.

This file is the only list of valid features (the database doesn't check them).
"""

from enum import StrEnum


class Feature(StrEnum):
    ORGANIZATION = "organization"
    USERS = "users"
    ROLES = "roles"
    PROJECTS = "projects"
    MEMBERS = "members"
    TEAMS = "teams"
    SPACES = "spaces"  # "delete" is never used: spaces can't be deleted (S-5)
    TASK_STATUSES = "task_statuses"
    TASKS = "tasks"
    COMMENTS = "comments"


class Action(StrEnum):
    VIEW = "view"
    CREATE = "create"
    EDIT = "edit"
    DELETE = "delete"

    @property
    def column(self) -> str:
        """The matching ``role_permissions`` column, e.g. ``view_perm``."""
        return f"{self.value}_perm"


# Features that only the user's organization-wide role can grant (A-5, rule 1).
ORG_FEATURES = {Feature.ORGANIZATION, Feature.USERS, Feature.ROLES}

ADMIN_ROLE_NAME = "Admin"


# is_org_scoped decides which permissions a project role is allowed to grant.
# Some permissions can only come from the user's organization-wide role.
#
# A user can have two kinds of role:
#   - user_roles.project_id IS NULL     -> organization-level role
#   - user_roles.project_id IS NOT NULL -> project-level role
#
# The problem it solves: some permissions are about the whole organization,
# not one project:
#   - organization:    change the org's settings
#   - users:           invite or remove people
#   - roles:           create roles and change their permissions
#   - projects.create: create a new project
# If a project role could grant these, a manager of one project could gain
# power over the whole organization (privilege escalation).
# isorganizationscoped means based on orgnization user role whole crud
# permission is with the organization level role
# and project level role will not have any permission for these features
def is_org_scoped(feature: Feature, action: Action) -> bool:
    """True if only the org-wide role counts for this permission.

    All other permissions can also come from the user's role in a project.
    Creating a project is org-wide too: no project role exists before it.
    """
    return feature in ORG_FEATURES or (feature, action) == (
        Feature.PROJECTS,
        Action.CREATE,
    )


# admin_permission_rows is used only once, at sign-up, and only for the Admin role
# (app/services/auth.py: register). It is NOT a default for every role:
#   - the Admin role created at sign-up gets every action on every feature (all True),
#     so the first user can manage the new organization;
#   - any role created later starts with every action False (the database default in
#     app/models/role.py) and gets only what an admin allows.
# It fills role_permissions (what a role can do), not user_roles (who has the role).
def admin_permission_rows() -> list[dict]:
    """Rows for the Admin role made at sign-up: every action on every feature (5.3).

    Example row: ``{"feature": "tasks", "view_perm": True, "create_perm": True,
    "edit_perm": True, "delete_perm": True}``. The caller adds ``role_id``.
    """
    rows = []
    for feature in Feature:
        row = {"feature": feature.value}
        for action in Action:
            row[action.column] = True
        rows.append(row)
    return rows
