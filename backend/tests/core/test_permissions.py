"""Features, actions and the default Admin role (app/core/permissions.py)."""

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import (
    ADMIN_ROLE_NAME,
    Action,
    Feature,
    admin_permission_rows,
    is_org_scoped,
)
from app.models import RolePermission
from app.models.role import FEATURE_MAX_LENGTH
from tests.factories import make_role


def test_the_ten_features_of_api_md_section_5_1() -> None:
    assert [feature.value for feature in Feature] == [
        "organization",
        "users",
        "roles",
        "projects",
        "members",
        "teams",
        "spaces",
        "task_statuses",
        "tasks",
        "comments",
    ]


def test_feature_names_fit_in_the_database_column() -> None:
    assert all(len(feature.value) <= FEATURE_MAX_LENGTH for feature in Feature)


def test_feature_from_a_stored_string() -> None:
    assert Feature("task_statuses") is Feature.TASK_STATUSES
    with pytest.raises(ValueError, match="'billing' is not a valid Feature"):
        Feature("billing")


def test_each_action_has_a_column_in_role_permissions() -> None:
    assert [action.column for action in Action] == [
        "view_perm",
        "create_perm",
        "edit_perm",
        "delete_perm",
    ]
    for action in Action:
        assert action.column in RolePermission.__table__.columns


@pytest.mark.parametrize("feature", [Feature.ORGANIZATION, Feature.USERS, Feature.ROLES])
@pytest.mark.parametrize("action", list(Action))
def test_organization_features_come_only_from_the_org_wide_role(
    feature: Feature, action: Action
) -> None:
    assert is_org_scoped(feature, action)


def test_only_creating_a_project_is_org_scoped() -> None:
    assert is_org_scoped(Feature.PROJECTS, Action.CREATE)
    for action in (Action.VIEW, Action.EDIT, Action.DELETE):
        assert not is_org_scoped(Feature.PROJECTS, action), action


@pytest.mark.parametrize(
    "feature",
    [
        Feature.MEMBERS,
        Feature.TEAMS,
        Feature.SPACES,
        Feature.TASK_STATUSES,
        Feature.TASKS,
        Feature.COMMENTS,
    ],
)
@pytest.mark.parametrize("action", list(Action))
def test_project_features_can_come_from_a_project_role(feature: Feature, action: Action) -> None:
    assert not is_org_scoped(feature, action)


def test_admin_role_has_every_action_on_every_feature() -> None:
    rows = admin_permission_rows()

    assert ADMIN_ROLE_NAME == "Admin"
    assert [row["feature"] for row in rows] == [feature.value for feature in Feature]
    for row in rows:
        assert row == {
            "feature": row["feature"],
            "view_perm": True,
            "create_perm": True,
            "edit_perm": True,
            "delete_perm": True,
        }


def test_admin_rows_are_new_lists_each_time() -> None:
    # The caller adds role_id to each row; that must not leak into the next sign-up.
    first = admin_permission_rows()
    first[0]["role_id"] = 1
    assert "role_id" not in admin_permission_rows()[0]


async def test_admin_rows_insert_into_the_database(db_session: AsyncSession) -> None:
    role = await make_role(db_session, name=ADMIN_ROLE_NAME)

    await db_session.execute(
        insert(RolePermission), [row | {"role_id": role.id} for row in admin_permission_rows()]
    )

    stored = (
        await db_session.scalars(select(RolePermission).where(RolePermission.role_id == role.id))
    ).all()
    assert len(stored) == 10
    assert all(
        permission.view_perm
        and permission.create_perm
        and permission.edit_perm
        and permission.delete_perm
        for permission in stored
    )
