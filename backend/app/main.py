"""The FastAPI application.

Run in development (from backend/, venv active):

    uvicorn app.main:app --reload

``create_app()`` builds the app; the module-level ``app`` is what uvicorn loads.
Tests call ``create_app()`` to get a fresh app.

Order of the layers around every request (outermost first):

1. Starlette's error middleware: turns an unexpected exception into our
   ``500 internal_error`` JSON (app/core/exceptions.py);
2. ``RequestIdMiddleware``: gives the request its id (log lines, error bodies,
   the ``X-Request-ID`` response header);
3. the router: ``/api/v1/...`` endpoints, with our handlers for 4xx errors.

No CORS middleware: browsers never call this API directly. The Next.js server
(BFF) calls it server-to-server and keeps the tokens in httpOnly cookies.
"""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import (
    configure_logging,
    new_request_id,
    reset_request_id,
    set_request_id,
)
from app.db.session import dispose_engine

API_PREFIX = "/api/v1"
REQUEST_ID_HEADER = "X-Request-ID"
# An id sent by the caller (e.g. the Next.js server) is reused only if it looks
# like an id. Anything else (too long, spaces, newlines) is replaced, so a
# caller can't inject fake lines or junk into our logs.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")


class RequestIdMiddleware:
    """Pure ASGI middleware (not ``BaseHTTPMiddleware``, which is slower and runs
    the endpoint in a different context, so the request id could get lost)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(REQUEST_ID_HEADER.lower().encode(), b"")
        decoded = incoming.decode("latin-1")
        request_id = decoded if _VALID_REQUEST_ID.fullmatch(decoded) else new_request_id()

        # request.state.request_id: still readable by the 500 handler, which
        # runs after this middleware has finished (app/core/exceptions.py).
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        token = set_request_id(request_id)
        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            reset_request_id(token)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Startup needs nothing: the database pool opens on first use, and
        # /health reports if the database is down instead of refusing to start.
        yield
        await dispose_engine()  # close pooled connections on shutdown

    # Interactive API docs only outside production (they list every endpoint).
    show_docs = not settings.is_production
    app = FastAPI(
        title="Task Management API",
        version="0.1.0",
        lifespan=lifespan,
        openapi_url=f"{API_PREFIX}/openapi.json" if show_docs else None,
        docs_url=f"{API_PREFIX}/docs" if show_docs else None,
        redoc_url=None,
    )
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router, prefix=API_PREFIX)
    return app


app = create_app()
