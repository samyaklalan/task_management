"""All version-1 routes, mounted under ``/api/v1`` by app/main.py.

Each feature adds its router here as it is built, e.g.
``api_router.include_router(auth.router)``.
"""

from fastapi import APIRouter

from app.api.v1 import auth, health, organization

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(organization.router)
