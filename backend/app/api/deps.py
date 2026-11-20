"""Dependencies shared by the routers.

    async def endpoint(session: SessionDep): ...

M3 adds the logged-in user and the permission check here.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session

# One database session per request (app/db/session.py); the service commits.
SessionDep = Annotated[AsyncSession, Depends(get_session)]
