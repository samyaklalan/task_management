"""Database access for spaces. Never commits (see app/repositories/user.py).

Grows with the spaces module (M10); for now only what a new project needs.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Space


async def create(session: AsyncSession, *, project_id: int, name: str, position: int) -> Space:
    space = Space(project_id=project_id, name=name, position=position)
    session.add(space)
    await session.flush()
    return space
