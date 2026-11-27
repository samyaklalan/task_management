"""Effective permissions (M3: app/services/authorization.py, api.md section 5.2)."""

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.permissions import Action, Feature, admin_permission_rows
from app.models import Organization, Project, RolePermission, User, UserRole
from app.repositories import role as role_repository
from app.services.authorization import load_permissions
from tests.factories import make_organization, make_project, make_role, make_user
from tests.utils.query_counter import count_queries

V, C, E, D = Action.VIEW, Action.CREATE, Action.EDIT, Action.DELETE


async def role_with(
    session: AsyncSession, organization: Organization, name: str, grants: dict[Feature, set]
) -> int:
    """A role allowing exactly ``grants``, e.g. ``{Feature.TASKS: {V, E}}``."""
    role = await make_role(session, organization=organization, name=name)
    for feature, actions in grants.items():
        session.add(
            RolePermission(
                role_id=role.id,
                feature=feature.value,
                **{action.column: action in actions for action in Action},
            )
        )
    await session.flush()
    return role.id


async def give(
    session: AsyncSession, user: User, role_id: int, project: Project | None = None
) -> None:
    session.add(
        UserRole(user_id=user.id, role_id=role_id, project_id=project.id if project else None)
    )
    await session.flush()


async def test_user_without_any_role_can_do_nothing(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    project = await make_project(db_session)

    permissions = await load_permissions(db_session, user.id, project.id)

    assert permissions.org_role is None and permissions.project_role is None
    assert not permissions.can_see_project
    assert not any(permissions.allows(f, a) for f in Feature for a in Action)


async def test_org_admin_can_do_everything_in_every_project(db_session: AsyncSession) -> None:
    organization = await make_organization(db_session)
    user = await make_user(db_session, organization=organization)
    admin = await make_role(db_session, organization=organization, name="Admin")
    await role_repository.add_permissions(db_session, admin.id, admin_permission_rows())
    await give(db_session, user, admin.id)
    project = await make_project(db_session, organization=organization)

    permissions = await load_permissions(db_session, user.id, project.id)

    assert permissions.org_role.name == "Admin" and permissions.project_role is None
    assert permissions.can_see_project
    assert all(permissions.allows(f, a) for f in Feature for a in Action)


async def test_org_and_project_roles_combine_with_or(db_session: AsyncSession) -> None:
    organization = await make_organization(db_session)
    user = await make_user(db_session, organization=organization)
    member = await role_with(db_session, organization, "Member", {Feature.TASKS: {V}})
    editor = await role_with(db_session, organization, "Editor", {Feature.TASKS: {E, C}})
    await give(db_session, user, member)
    project = await make_project(db_session, organization=organization)
    other_project = await make_project(db_session, organization=organization)
    await give(db_session, user, editor, project)

    here = await load_permissions(db_session, user.id, project.id)
    elsewhere = await load_permissions(db_session, user.id, other_project.id)

    assert here.project_role.name == "Editor"
    assert {a for a in Action if here.allows(Feature.TASKS, a)} == {V, C, E}
    # The Editor role belongs to the other project only: there it's just Member.
    assert elsewhere.project_role is None
    assert {a for a in Action if elsewhere.allows(Feature.TASKS, a)} == {V}


async def test_a_weaker_project_role_never_takes_away_org_rights(
    db_session: AsyncSession,
) -> None:
    organization = await make_organization(db_session)
    user = await make_user(db_session, organization=organization)
    full = await role_with(db_session, organization, "Lead", {Feature.TASKS: {V, C, E, D}})
    viewer = await role_with(db_session, organization, "Viewer", {Feature.TASKS: {V}})
    project = await make_project(db_session, organization=organization)
    await give(db_session, user, full)
    await give(db_session, user, viewer, project)

    permissions = await load_permissions(db_session, user.id, project.id)

    assert permissions.allows(Feature.TASKS, D)


async def test_org_features_never_come_from_a_project_role(db_session: AsyncSession) -> None:
    organization = await make_organization(db_session)
    user = await make_user(db_session, organization=organization)
    everything = await role_with(
        db_session, organization, "Project boss", {f: {V, C, E, D} for f in Feature}
    )
    project = await make_project(db_session, organization=organization)
    await give(db_session, user, everything, project)

    permissions = await load_permissions(db_session, user.id, project.id)

    for feature in (Feature.ORGANIZATION, Feature.USERS, Feature.ROLES):
        assert not any(permissions.allows(feature, a) for a in Action), feature
    assert not permissions.allows(Feature.PROJECTS, C)  # creating a project is org-wide
    # ...but the project-level parts of "projects" do come from the project role.
    assert permissions.allows(Feature.PROJECTS, V) and permissions.allows(Feature.PROJECTS, E)
    assert permissions.allows(Feature.TASKS, D)


async def test_a_role_without_permission_rows_still_counts_as_access(
    db_session: AsyncSession,
) -> None:
    organization = await make_organization(db_session)
    user = await make_user(db_session, organization=organization)
    empty = await make_role(db_session, organization=organization, name="Guest")
    project = await make_project(db_session, organization=organization)
    await give(db_session, user, empty.id, project)

    permissions = await load_permissions(db_session, user.id, project.id)

    assert permissions.project_role.name == "Guest"
    assert permissions.can_see_project  # the project exists for them (403s, not 404s)
    assert not any(permissions.allows(f, a) for f in Feature for a in Action)


async def test_an_unknown_stored_feature_is_ignored(db_session: AsyncSession) -> None:
    organization = await make_organization(db_session)
    user = await make_user(db_session, organization=organization)
    role = await role_with(db_session, organization, "Member", {Feature.TASKS: {V}})
    db_session.add(RolePermission(role_id=role, feature="billing", view_perm=True))
    await give(db_session, user, role)

    permissions = await load_permissions(db_session, user.id)

    assert permissions.allows(Feature.TASKS, V)
    assert not permissions.allows(Feature.TASKS, E)


async def test_permissions_are_loaded_in_one_query(
    engine: AsyncEngine, db_session: AsyncSession
) -> None:
    organization = await make_organization(db_session)
    user = await make_user(db_session, organization=organization)
    member = await role_with(db_session, organization, "Member", {f: {V} for f in Feature})
    editor = await role_with(db_session, organization, "Editor", {f: {E} for f in Feature})
    project = await make_project(db_session, organization=organization)
    await give(db_session, user, member)
    await give(db_session, user, editor, project)

    with count_queries(engine) as queries:
        await load_permissions(db_session, user.id, project.id)

    assert queries.count == 1, str(queries)
