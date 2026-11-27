"""AUTH-1 register and AUTH-2 login (docs/api.md, section 8).

Routers stay thin: validate the body (the schema does that), call the
service, return its result. Token responses are never cached.
"""

from fastapi import APIRouter, Response, status

from app.api.deps import SessionDep
from app.schemas.auth import LoginRequest, LoginResponse, RegisterRequest
from app.schemas.common import ErrorResponse
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    summary="Sign up: new organization and its first admin (AUTH-1)",
    responses={409: {"model": ErrorResponse, "description": "Email or slug already taken"}},
)
async def register(body: RegisterRequest, session: SessionDep, response: Response) -> LoginResponse:
    response.headers["Cache-Control"] = "no-store"
    return await auth_service.register(session, body)


@router.post(
    "/login",
    summary="Log in with email and password (AUTH-2)",
    responses={401: {"model": ErrorResponse, "description": "invalid_credentials"}},
)
async def login(body: LoginRequest, session: SessionDep, response: Response) -> LoginResponse:
    response.headers["Cache-Control"] = "no-store"
    return await auth_service.login(session, body)
