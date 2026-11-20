"""Database access for organizations. Never commits (see app/repositories/user.py)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Organization


async def create(session: AsyncSession, *, name: str, slug: str) -> Organization:
    organization = Organization(name=name, slug=slug)
    session.add(organization)
    await session.flush()  # raises IntegrityError (uq_organizations_slug) if the slug is taken
    return organization


async def get_by_id(session: AsyncSession, organization_id: int) -> Organization | None:
    return await session.get(Organization, organization_id)
