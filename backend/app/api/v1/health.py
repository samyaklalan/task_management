"""SYS-1 ``GET /api/v1/health`` (docs/api.md, section 8).

Used by load balancers and uptime monitors: no authentication, no permission,
one ``SELECT 1``. It answers within ``HEALTH_TIMEOUT_SECONDS`` even when the
database hangs (for example, a network outage where connecting would otherwise
wait much longer), because a health check that hangs is worse than one that
says "unavailable".
"""

import logging

import anyio
from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from app.db import session as db_session

logger = logging.getLogger(__name__)

HEALTH_TIMEOUT_SECONDS = 3.0

router = APIRouter(tags=["system"])


class HealthOk(BaseModel):
    status: str = "ok"
    database: str = "ok"


class HealthUnavailable(BaseModel):
    status: str = "unavailable"


@router.get(
    "/health",
    response_model=HealthOk,
    summary="Health check (SYS-1)",
    responses={
        503: {"model": HealthUnavailable, "description": "Database unreachable"}
    },
)
async def health(response: Response) -> HealthOk | Response:
    # Never cached by a proxy: every call must really check the database.
    response.headers["Cache-Control"] = "no-store"

    database_ok = False
    with anyio.move_on_after(HEALTH_TIMEOUT_SECONDS) as timeout:
        database_ok = await db_session.ping_database()
    if timeout.cancelled_caught:
        logger.warning(
            "Health check: database did not answer within %ss", HEALTH_TIMEOUT_SECONDS
        )

    if database_ok:
        return HealthOk()
    return Response(
        content=HealthUnavailable().model_dump_json(),
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )
