"""Spaces of a project (docs/api.md, SPC-1 to SPC-5). No delete (S-5)."""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ConflictError,
    InvalidReferenceError,
    NotFoundError,
    StaleUpdateError,
)
from app.db.errors import constraint_name
from app.models import Project, Space
from app.repositories import space as space_repository
from app.schemas.common import Page
from app.schemas.space import (
    SPACES_PAGE_SIZE,
    CreateSpaceRequest,
    ReorderSpacesRequest,
    SpaceOut,
    UpdateSpaceRequest,
)
from app.utils.pagination import PageParams

_NAME_TAKEN = "A space with this name already exists in this project."


async def list_spaces(
    session: AsyncSession, project: Project, *, q: str | None, page: PageParams
) -> Page[SpaceOut]:
    rows, pagination = await space_repository.list_spaces(
        session, project_id=project.id, q=q, page=page
    )
    return Page[SpaceOut](
        items=[_out(space, count) for space, count in rows], pagination=pagination
    )


async def get_space(session: AsyncSession, project: Project, space_id: int) -> SpaceOut:
    row = await space_repository.get_with_count(session, project.id, space_id)
    if row is None:
        raise NotFoundError("Space")
    space, count = row
    return _out(space, count)


async def create_space(
    session: AsyncSession, project: Project, data: CreateSpaceRequest
) -> SpaceOut:
    """At the end of the sidebar: position = current max + 1, computed under a
    per-project lock so two simultaneous creates get different positions."""
    await space_repository.lock_layout(session, project.id)
    position = await space_repository.next_position(session, project.id)
    try:
        async with session.begin_nested():
            space = await space_repository.create(
                session,
                project_id=project.id,
                name=data.name,
                description=data.description,
                position=position,
            )
    except IntegrityError as exc:
        _raise_if_name_taken(exc)
        raise
    await session.commit()
    return _out(space, 0)


async def update_space(
    session: AsyncSession, project: Project, space_id: int, data: UpdateSpaceRequest
) -> SpaceOut:
    """Rename and/or describe. Tasks keep their space_id, so they follow."""
    space = await _get(session, project, space_id)
    if space.updated_at != data.expected_updated_at:
        raise StaleUpdateError(current=await _current(session, space))

    values = data.model_dump(include=data.model_fields_set - {"expected_updated_at"})
    if values:
        try:
            async with session.begin_nested():
                updated = await space_repository.update_if_unchanged(
                    session,
                    space.id,
                    expected_updated_at=data.expected_updated_at,
                    values=values,
                )
        except IntegrityError as exc:
            _raise_if_name_taken(exc)
            raise
        if updated is None:  # saved by someone else between our SELECT and UPDATE
            current = await _get(session, project, space_id)
            raise StaleUpdateError(current=await _current(session, current))
        space = updated
        await session.commit()
    return _out(space, await space_repository.count_tasks(session, space.id))


async def reorder_spaces(
    session: AsyncSession, project: Project, data: ReorderSpacesRequest
) -> Page[SpaceOut]:
    """SPC-5: positions become 0, 1, 2, … in the order given. The list must be
    exactly the project's spaces (checked against the locked rows). Only rows
    whose position changes are written, so repeating the same order is a no-op."""
    await space_repository.lock_layout(session, project.id)
    current = await space_repository.lock_positions(session, project.id)

    duplicates = len(data.space_ids) != len(set(data.space_ids))
    if duplicates or set(data.space_ids) != set(current):
        missing = sorted(set(current) - set(data.space_ids))
        unknown = sorted(set(data.space_ids) - set(current))
        problems = [
            f"missing: {', '.join(map(str, missing))}" if missing else "",
            f"not in this project: {', '.join(map(str, unknown))}" if unknown else "",
            "an id appears more than once" if duplicates else "",
        ]
        raise InvalidReferenceError(
            "space_ids",
            "space_ids must list every space of this project exactly once ("
            + "; ".join(p for p in problems if p)
            + ").",
        )

    changed = {
        space_id: position
        for position, space_id in enumerate(data.space_ids)
        if current[space_id] != position
    }
    await space_repository.set_positions(session, changed)
    await session.commit()
    return await list_spaces(
        session, project, q=None, page=PageParams(page=1, page_size=SPACES_PAGE_SIZE)
    )


# ── Shared ────────────────────────────────────────────────────────────────────


def _raise_if_name_taken(exc: IntegrityError) -> None:
    if constraint_name(exc) == "uq_spaces_project_id_name":
        raise ConflictError(_NAME_TAKEN, field="name") from exc


async def _get(session: AsyncSession, project: Project, space_id: int) -> Space:
    space = await space_repository.get(session, project.id, space_id)
    if space is None:
        raise NotFoundError("Space")
    return space


async def _current(session: AsyncSession, space: Space) -> dict:
    count = await space_repository.count_tasks(session, space.id)
    return _out(space, count).model_dump(mode="json")


def _out(space: Space, task_count: int) -> SpaceOut:
    return SpaceOut(
        id=space.id,
        name=space.name,
        description=space.description,
        position=space.position,
        task_count=task_count,
        created_at=space.created_at,
        updated_at=space.updated_at,
    )
