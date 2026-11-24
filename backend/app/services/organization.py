"""The caller's organization (docs/api.md, ORG-1 and ORG-2)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, StaleUpdateError
from app.models import Organization
from app.repositories import organization as organization_repository
from app.schemas.organization import OrganizationOut, UpdateOrganizationRequest


async def get(session: AsyncSession, organization_id: int) -> OrganizationOut:
    organization = await _load(session, organization_id)
    return OrganizationOut.model_validate(organization)


# vaidating the output with orgabnization schema

# truy ro get the org by id using load function it
# retiuns organization else raise eror


# How update works:
#   1. values = only the fields the client sent (name and/or description),
#      without expected_updated_at.
#   2. Values sent -> update_if_unchanged updates the organization, but only if
#      updated_at still equals expected_updated_at. Returns the updated
#      organization, or None (someone else saved first).
#   3. No values sent -> NOTHING is updated. _load gets the organization:
#      updated_at == expected_updated_at -> updated = current (unchanged);
#      otherwise -> updated = None.
#   4. updated is None -> rollback, load the current organization, raise 409
#      stale_update with it in details.current.
#   5. Otherwise -> commit and return the organization (200).
# It compares the updated_at TIME, not the values: sending the same name again
# still updates, as long as updated_at matches.
async def update(
    session: AsyncSession, organization_id: int, data: UpdateOrganizationRequest
) -> OrganizationOut:
    """Change name and/or description, only if nobody else saved in between.

    On a conflict the answer is ``409 stale_update`` with the current version
    in ``details.current``, so the page can show it without another request.
    """
    # Only the fields the client actually sent ("description": null clears it).
    values = data.model_dump(include=data.model_fields_set - {"expected_updated_at"})
    # or use the exclude undset as true or model.dump
    if values:
        updated = await organization_repository.update_if_unchanged(
            session,
            organization_id,
            expected_updated_at=data.expected_updated_at,
            values=values,
        )
    else:
        # Nothing to change: just confirm the caller's copy is still current.
        # load to get the orhization first
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
