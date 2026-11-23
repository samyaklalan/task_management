"""The caller's organization (docs/api.md, ORG-1 and ORG-2)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, StaleUpdateError
from app.models import Organization
from app.repositories import organization as organization_repository
from app.schemas.organization import OrganizationOut, UpdateOrganizationRequest


async def get(session: AsyncSession, organization_id: int) -> OrganizationOut:
    organization = await _load(session, organization_id)
    return OrganizationOut.model_validate(organization)


async def update(
    session: AsyncSession, organization_id: int, data: UpdateOrganizationRequest
) -> OrganizationOut:
    """Change name and/or description, only if nobody else saved in between.

    On a conflict the answer is ``409 stale_update`` with the current version
    in ``details.current``, so the page can show it without another request.
    """
    # Only the fields the client actually sent ("description": null clears it).
    values = data.model_dump(include=data.model_fields_set - {"expected_updated_at"})

    if values:
        updated = await organization_repository.update_if_unchanged(
            session,
            organization_id,
            expected_updated_at=data.expected_updated_at,
            values=values,
        )
    else:
        # Nothing to change: just confirm the caller's copy is still current.
        current = await _load(session, organization_id)
        updated = current if current.updated_at == data.expected_updated_at else None

    if updated is None:
        await session.rollback()
        current = await _load(session, organization_id)
        raise StaleUpdateError(
            current=OrganizationOut.model_validate(current).model_dump(mode="json")
        )

    await session.commit()
    return OrganizationOut.model_validate(updated)


async def _load(session: AsyncSession, organization_id: int) -> Organization:
    organization = await organization_repository.get_by_id(session, organization_id)
    if organization is None:  # can't happen for a logged-in user, but never return None
        raise NotFoundError("Organization")
    return organization
