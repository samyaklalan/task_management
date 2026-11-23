"""Database access for organizations. Never commits (see app/repositories/user.py)."""

from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Organization


async def create(session: AsyncSession, *, name: str, slug: str) -> Organization:
    organization = Organization(name=name, slug=slug)
    session.add(organization)
    await session.flush()  # raises IntegrityError (uq_organizations_slug) if the slug is taken
    return organization


async def get_by_id(session: AsyncSession, organization_id: int) -> Organization | None:
    return await session.get(Organization, organization_id)


# added for the organization to update the organization now
async def update_if_unchanged(
    session: AsyncSession,
    organization_id: int,
    *,
    expected_updated_at: datetime,
    values: dict[str, Any],
) -> Organization | None:
    """Optimistic update (api.md section 6): one UPDATE that only matches if
    nobody saved since the caller loaded the row. ``None`` means "changed by
    someone else" (or gone). ``updated_at`` moves on automatically.

    Check and write are one statement, so two simultaneous saves can't both win.
    """
    return await session.scalar(
        update(Organization)
        .where(
            Organization.id == organization_id,
            Organization.updated_at == expected_updated_at,
        )
        .values(**values)
        .returning(Organization)
        .execution_options(populate_existing=True)
    )


# updates the organization chosen by id.
# values is a dict[str, Any] of the columns to change.
# It returns the Organization object.

# update_if_unchanged runs this one query:
#
#   UPDATE organizations
#   SET <values>                                -- e.g. name = 'New Name'
#   WHERE id = organization_id
#     AND updated_at = expected_updated_at      -- check: nobody saved since you loaded it
#   RETURNING *;                                -- give back the updated row
#
# It returns the updated Organization, or None if the WHERE didn't match
# (someone else saved first, or the organization is gone).
#
