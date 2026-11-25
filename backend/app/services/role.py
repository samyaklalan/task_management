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


# How list_roles works (repository code: app/repositories/role.py)
# ----------------------------------------------------------------
# 1. The client sends only GET /roles + token. No role name: organization_id comes
#    from the logged-in user, so nobody sees another organization's roles.
#
# 2. role_repository.list_with_details(session, organization_id):
#
#    def _with_details(organization_id: int) -> Select:
#        """Shared base query for list_with_details and get_with_details."""
#        # One number per role: how many users hold it.
#        # scalar_subquery: the inner SELECT returns ONE value, used as a column.
#        # correlate(Role): links to the OUTER query's current role, so each role
#        # gets its own count (not one total for every row).
#        assigned_count = (
#            select(func.count())
#            .select_from(UserRole)
#            .where(UserRole.role_id == Role.id)
#            .correlate(Role)
#            .scalar_subquery()
#        )
#        return (
#            select(Role, assigned_count.label("assigned_count"))
#            .where(Role.organization_id == organization_id)  # only this org's roles
#            .options(selectinload(Role.permissions))  # permissions in a 2nd query (IN)
#        )
#
#    async def list_with_details(session, organization_id) -> list[tuple[Role, int]]:
#        """ROLE-1: every role of the organization with its permissions and count."""
#        result = await session.execute(
#            # lower(name): case-insensitive sort; id last: always the same order
#            _with_details(organization_id).order_by(func.lower(Role.name), Role.id)
#        )
#        return [(role, count) for role, count in result.all()]
#
# 3. SQL it runs (2 queries, no N+1):
#    SELECT roles.*,
#           (SELECT count(*) FROM user_roles
#            WHERE user_roles.role_id = roles.id) AS assigned_count
#    FROM roles WHERE roles.organization_id = :org_id
#    ORDER BY lower(roles.name), roles.id;
#    SELECT * FROM role_permissions WHERE role_id IN (3, 5, 7);  -- selectinload
#
# 4. Result: [(Role Admin, 1), (Role Team Lead, 4), ...]
#    -> "for role, count in rows" unpacks each tuple
#    -> _role_out(role, count) builds RoleOut (always all 10 features)
#    -> RoleListResponse(items=[...]) is sent as {"items": [...]}


async def create_role(
    session: AsyncSession, organization_id: int, data: CreateRoleRequest
) -> RoleOut:
    """The role and a row for each of the ten features, in one transaction."""
    role = await role_repository.create(session, organization_id=organization_id, name=data.name)
    await role_repository.add_permissions(session, role.id, _all_ten(data.permissions))
    await session.commit()
    return await _get(session, organization_id, role.id)


# How create_role works (ROLE-2, repository code: app/repositories/role.py)
# -------------------------------------------------------------------------
# 0. Router (app/api/v1/roles.py) - runs BEFORE this service:
#
#    @router.post(
#        "",                                   # prefix "/roles" -> POST /roles
#        status_code=status.HTTP_201_CREATED,  # success = 201 Created
#        summary="Create a role with its permissions (ROLE-2)",
#        dependencies=[require_org_permission(Feature.ROLES, Action.CREATE)],
#        responses={403: {"model": ErrorResponse}},
#    )
#    async def create_role(
#        body: CreateRoleRequest, session: SessionDep, user: CurrentUserDep
#    ) -> RoleOut:
#        return await role_service.create_role(session, user.organization_id, body)
#
#    What FastAPI does, in order, before the service runs:
#    - CurrentUserDep: reads the token -> the logged-in user (401 if missing/invalid).
#    - require_org_permission(ROLES, CREATE): the user's org-wide role must have
#      roles.create, otherwise 403.
#    - body: CreateRoleRequest validates the JSON, otherwise 422.
#    - SessionDep: one AsyncSession for this request (rule 7).
#    The router is thin: no logic, it only passes user.organization_id and body on.
#    The returned RoleOut is sent as JSON with status 201.
#
# 1. The client sends POST /roles + token:
#        {"name": "Team Lead", "permissions": [{"feature": "tasks", "view": true}]}
#    CreateRoleRequest has already checked it: name 1..50 chars (spaces stripped),
#    no unknown fields (extra="forbid"), no feature listed twice.
#    organization_id comes from the logged-in user, not from the body.
#
# 2. role_repository.create: add the role and flush() -> role.id exists, nothing
#    committed yet.
#
#    async def create(session, *, organization_id: int, name: str) -> Role:
#        role = Role(organization_id=organization_id, name=name)
#        session.add(role)
#        await session.flush()  # sends the INSERT now: role.id is known
#        return role
#
# 3. _all_ten(data.permissions): ALWAYS 10 rows, one per feature. A listed feature
#    keeps its switches; every other feature gets all four switches False:
#        [{"feature": "organization", "view_perm": False, ...},
#         ...
#         {"feature": "tasks", "view_perm": True, "create_perm": False, ...}]
#
# 4. role_repository.add_permissions: SETS role_id on each row (no check), then ONE
#    INSERT for all 10 rows:
#
#    async def add_permissions(session, role_id: int, rows: list[dict]) -> None:
#        await session.execute(insert(RolePermission), _with_role_id(role_id, rows))
#
#    def _with_role_id(role_id: int, rows: list[dict]) -> list[dict]:
#        rows_with_role = []
#        for row in rows:
#            new_row = dict(row)  # copy, so the caller's row isn't changed
#            new_row["role_id"] = role_id  # SET (not get)
#            rows_with_role.append(new_row)
#        return rows_with_role
#
# 5. session.commit(): role + its 10 permission rows are saved TOGETHER (one
#    transaction). If anything failed before, nothing is saved.
#
# 6. _get -> get_with_details: reads the new role back with its permissions and
#    assigned_count (0 for a new role) and returns RoleOut.
#
# SQL it runs:
#    INSERT INTO roles (organization_id, name) VALUES (:org_id, 'Team Lead')
#        RETURNING id, created_at, updated_at;
#    INSERT INTO role_permissions (role_id, feature, view_perm, ...) VALUES
#        (:role_id, 'organization', false, ...), ... 10 rows;
#    COMMIT;
#    + the 2 SELECTs of get_with_details (role + count, then permissions IN).


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


# How update_role works (ROLE-3, repository code: app/repositories/role.py)
# -------------------------------------------------------------------------
# 0. Router (app/api/v1/roles.py) - runs BEFORE this service:
#
#    @router.patch(
#        "/{role_id}",                         # PATCH /roles/5 -> role_id = 5
#        summary="Rename a role and/or replace its permissions (ROLE-3)",
#        dependencies=[require_org_permission(Feature.ROLES, Action.EDIT)],
#        responses={
#            403: {"model": ErrorResponse},
#            404: {"model": ErrorResponse},
#            409: {"model": ErrorResponse, "description": "stale_update or self_lockout"},
#        },
#    )
#    async def update_role(
#        role_id: IdPath,                  # from the URL path, must be a positive id
#        body: UpdateRoleRequest,          # JSON body, validated (422 if wrong)
#        session: SessionDep,              # one session per request
#        user: CurrentUserDep,             # token -> logged-in user (401)
#        permissions: OrgPermissionsDep,   # caller's org-wide role (for step 2)
#    ) -> RoleOut:
#        return await role_service.update_role(
#            session, user.organization_id, role_id, body, permissions
#        )
#
#    require_org_permission(ROLES, EDIT): caller's org-wide role needs roles.edit (403).
#
# 1. The client sends PATCH /roles/5 + token:
#        {"name": "Lead", "permissions": [...], "expected_updated_at": "2026-11-24T10:00:00Z"}
#    name / permissions may be left out (= not changed), but not sent as null (422).
#    expected_updated_at = the updated_at the client saw when it loaded the role.
#
# 2. _get(...): 404 if role 5 doesn't exist or belongs to ANOTHER organization.
#
# 3. Self-lockout check (A-13): if the caller HOLDS this role and sends permissions
#    without roles.edit -> 409 self_lockout. Otherwise nobody could edit roles any more.
#
# 4. update_if_unchanged: rename + check the version in ONE statement.
#
#    return await session.scalar(
#        update(Role)
#        .where(Role.id == role_id, Role.updated_at == expected_updated_at)
#        .values(**values, updated_at=func.statement_timestamp())  # bulk: set it ourselves
#        .returning(Role)
#        .execution_options(populate_existing=True)  # refresh the cached Role object
#    )
#
#    - values may be {} (only permissions change): the UPDATE still runs, so updated_at
#      moves on and the version is still checked.
#    - No row matched (someone saved in between) -> None -> rollback, load the current
#      role and raise 409 stale_update with it, so the client can show the newer data.
#
# 5. replace_permissions (only if permissions were sent): _all_ten makes 10 rows, so
#    features NOT listed are switched OFF (complete list, no merge). One upsert:
#
#    stmt = pg_insert(RolePermission).values(_with_role_id(role_id, rows))
#    await session.execute(
#        stmt.on_conflict_do_update(
#            index_elements=[RolePermission.role_id, RolePermission.feature],
#            set_={  # excluded = the row we tried to insert
#                "view_perm": stmt.excluded.view_perm,
#                "create_perm": stmt.excluded.create_perm,
#                "edit_perm": stmt.excluded.edit_perm,
#                "delete_perm": stmt.excluded.delete_perm,
#            },
#        )
#    )
#    Existing row -> updated in place; missing row -> inserted. Never delete + insert
#    (that collides with uq_role_permissions_role_id_feature).
#
# 6. session.commit(): rename + permissions saved TOGETHER.
#
# 7. _get -> get_with_details with populate_existing=True: the Role from step 2 is
#    still cached with OLD permissions; populate_existing overwrites it, so the
#    response shows the NEW permissions and the new updated_at.
#
# SQL it runs:
#    SELECT ... (step 2, role + count; then permissions IN)
#    UPDATE roles SET name = 'Lead', updated_at = statement_timestamp()
#        WHERE id = 5 AND updated_at = :expected RETURNING ...;
#    INSERT INTO role_permissions (...) VALUES (... 10 rows ...)
#        ON CONFLICT (role_id, feature) DO UPDATE SET view_perm = excluded.view_perm, ...;
#    COMMIT;
#    SELECT ... (step 7, fresh role + count; then permissions IN)


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
