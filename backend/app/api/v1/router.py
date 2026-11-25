"""All version-1 routes, mounted under ``/api/v1`` by app/main.py.

Each feature adds its router here as it is built, e.g.
``api_router.include_router(auth.router)``.
"""

from fastapi import APIRouter

from app.api.v1 import (
    auth,
    health,
    members,
    organization,
    projects,
    roles,
    teams,
    users,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(organization.router)
api_router.include_router(users.router)
api_router.include_router(roles.router)

api_router.include_router(projects.router)
api_router.include_router(members.router)
api_router.include_router(teams.router)
