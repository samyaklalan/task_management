"""Roles and their permissions (docs/api.md, ROLE-1 to ROLE-4)."""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    NotFoundError,
    RoleInUseError,
    SelfLockoutError,
    StaleUpdateError,
)
from app.core.permissions import Action, Feature
from app.models import Role
from app.repositories import role as role_repository
from app.schemas.role import (
    CreateRoleRequest,
    FeaturePermissions,
    RoleListResponse,
    RoleOut,
    UpdateRoleRequest,
)
from app.services.authorization import Permissions


async def list_roles(session: AsyncSession, organization_id: int) -> RoleListResponse:
    rows = await role_repository.list_with_details(session, organization_id)
    return RoleListResponse(items=[_role_out(role, count) for role, count in rows])


async def create_role(
    session: AsyncSession, organization_id: int, data: CreateRoleRequest
) -> RoleOut:
    """The role and a row for each of the ten features, in one transaction."""
    role = await role_repository.create(session, organization_id=organization_id, name=data.name)
    await role_repository.add_permissions(session, role.id, _all_ten(data.permissions))
    await session.commit()
    return await _get(session, organization_id, role.id)


async def update_role(
    session: AsyncSession,
    organization_id: int,
    role_id: int,
    data: UpdateRoleRequest,
    caller_permissions: Permissions,
) -> RoleOut:
    """Rename and/or replace the permissions, only if nobody else saved in between."""
    await _get(session, organization_id, role_id)  # 404 if not in this organization

    holds_this_role = (
        caller_permissions.org_role is not None and caller_permissions.org_role.id == role_id
    )
    if holds_this_role and data.permissions is not None:
        new_roles = next((p for p in data.permissions if p.feature is Feature.ROLES), None)
        if new_roles is None or not new_roles.edit:
            # A-13: otherwise nobody (including you) could fix roles any more.
            raise SelfLockoutError("You cannot remove 'roles.edit' from the role you hold.")

    values = {"name": data.name} if data.name is not None else {}
    saved = await role_repository.update_if_unchanged(
        session, role_id, expected_updated_at=data.expected_updated_at, values=values
    )
    if not saved:
        await session.rollback()
        current = await _get(session, organization_id, role_id)
        raise StaleUpdateError(current=current.model_dump(mode="json"))

    if data.permissions is not None:
        await role_repository.replace_permissions(session, role_id, _all_ten(data.permissions))
    await session.commit()
    return await _get(session, organization_id, role_id)


async def delete_role(session: AsyncSession, organization_id: int, role_id: int) -> None:
    """Delete a role nobody holds. The database refuses while it's assigned
    (fk_user_roles_role_id_roles), so a role assigned a moment ago is safe too."""
    await _get(session, organization_id, role_id)
    try:
        # A savepoint: if the delete fails, only it is undone, and the count
        # below still runs in the same transaction.
        async with session.begin_nested():
            await role_repository.delete_role(session, role_id)
    except IntegrityError as exc:
        count = await role_repository.count_assignments(session, role_id)
        raise RoleInUseError(assigned_count=count) from exc
    await session.commit()


async def _get(session: AsyncSession, organization_id: int, role_id: int) -> RoleOut:
    found = await role_repository.get_with_details(session, organization_id, role_id)
    if found is None:  # unknown, or another organization's role
        raise NotFoundError("Role")
    return _role_out(*found)


def _all_ten(permissions: list[FeaturePermissions]) -> list[dict]:
    """Rows for every feature: the listed ones as given, the others all off."""
    given = {item.feature: item for item in permissions}
    rows = []
    for feature in Feature:
        item = given.get(feature)
        row = {"feature": feature.value}
        for action in Action:
            row[action.column] = bool(item and getattr(item, action.value))
        rows.append(row)
    return rows


def _role_out(role: Role, assigned_count: int) -> RoleOut:
    """Always all ten features in a fixed order; a missing row means all off."""
    stored = {row.feature: row for row in role.permissions}
    permissions = []
    for feature in Feature:
        row = stored.get(feature.value)
        flags = {action.value: bool(row and getattr(row, action.column)) for action in Action}
        permissions.append(FeaturePermissions(feature=feature, **flags))
    return RoleOut(
        id=role.id,
        name=role.name,
        assigned_count=assigned_count,
        permissions=permissions,
        created_at=role.created_at,
        updated_at=role.updated_at,
    )
