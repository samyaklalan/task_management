"""Application errors and the handlers that turn them into JSON responses.

Every error the API returns has the same shape (docs/api.md, section 3):

    {
      "error": {"code": "not_found", "message": "Project not found.", "details": ...},
      "request_id": "0f6c2a1e9b7d4c35"
    }

Services raise the ``AppError`` subclasses below; they never build HTTP
responses themselves. ``register_exception_handlers(app)`` (called from
app/main.py) converts:

* ``AppError``                -> its own status code and ``code``
* request validation errors   -> 422 ``validation_error`` with per-field details
                                 (400 ``bad_request`` if the body isn't valid JSON)
* Starlette HTTP errors       -> e.g. unknown URL 404, wrong method 405
* anything else (a bug)       -> 500 ``internal_error``; full traceback in the
                                 log only, never in the response
"""

import logging
from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_request_id, reset_request_id, set_request_id

logger = logging.getLogger(__name__)

ErrorDetails = list[dict[str, Any]] | dict[str, Any] | None


# ── Base class ────────────────────────────────────────────────────────────────


class AppError(Exception):
    """Base for every expected error. Subclasses set the three class attributes."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"
    message: str = "The request could not be processed."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: ErrorDetails = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details
        self.headers = headers
        super().__init__(self.message)


# ── 400 / 401 / 403 / 404 ─────────────────────────────────────────────────────


class BadRequestError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "bad_request"
    message = "The request could not be processed."


class _UnauthorizedError(AppError):
    """401 errors tell the client which auth scheme to use."""

    status_code = status.HTTP_401_UNAUTHORIZED

    def __init__(self, message: str | None = None, *, details: ErrorDetails = None) -> None:
        super().__init__(message, details=details, headers={"WWW-Authenticate": "Bearer"})


class NotAuthenticatedError(_UnauthorizedError):
    code = "not_authenticated"
    message = "Authentication is required."


class InvalidCredentialsError(_UnauthorizedError):
    # One message for unknown email, wrong password and inactive account,
    # so the response never reveals which accounts exist.
    code = "invalid_credentials"
    message = "Invalid email or password."


class InvalidTokenError(_UnauthorizedError):
    code = "invalid_token"
    message = "The token is invalid or has expired."


class TokenReusedError(_UnauthorizedError):
    code = "token_reused"
    message = "This session is no longer valid. Please log in again."


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "You do not have permission to perform this action."


class NotFoundError(AppError):
    """Also used when the caller has no access to a project (404 before 403)."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "Resource not found."

    def __init__(self, resource: str | None = None, *, details: ErrorDetails = None) -> None:
        super().__init__(f"{resource} not found." if resource else None, details=details)


# ── 409 Conflict ──────────────────────────────────────────────────────────────


class ConflictError(AppError):
    """A unique rule was violated, e.g. email or task prefix already used."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "A resource with these values already exists."

    def __init__(
        self,
        message: str | None = None,
        *,
        field: str | None = None,
        details: ErrorDetails = None,
    ) -> None:
        if field is not None and details is None:
            details = [{"field": field, "message": message or "Already in use."}]
        super().__init__(message, details=details)


class StaleUpdateError(AppError):
    """``expected_updated_at`` did not match: someone else saved first."""

    status_code = status.HTTP_409_CONFLICT
    code = "stale_update"
    message = "This item was changed by someone else. Reload it and try again."

    def __init__(self, current: Any = None) -> None:
        # ``current`` is the latest version of the resource, so the UI can reload.
        super().__init__(details={"current": current} if current is not None else None)


class ProjectArchivedError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "project_archived"
    message = "This project is archived and read-only."


class StatusInUseError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "status_in_use"
    message = "Tasks still use this status. Move them to another status first."

    def __init__(self, task_count: int) -> None:
        super().__init__(details={"task_count": task_count})


class LastStatusError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "last_status"
    message = "A project must keep at least one status."


class RoleInUseError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "role_in_use"
    message = "This role is still assigned to users."

    def __init__(self, assigned_count: int) -> None:
        super().__init__(details={"assigned_count": assigned_count})


class AlreadyMemberError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "already_member"
    message = "This user already has a role in this project."


class TokenAlreadyRotatedError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "token_already_rotated"
    message = "The session was refreshed by another request. Retry with the new session."


class SelfLockoutError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "self_lockout"
    message = "You cannot remove your own administrative access."


# ── 422 ───────────────────────────────────────────────────────────────────────


class SearchTooShortError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "search_too_short"
    message = "Search text must be at least 3 characters."


class InvalidReferenceError(AppError):
    """A referenced id exists but can't be used here (wrong project or organization)."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "invalid_reference"
    message = "A referenced item is not valid here."

    def __init__(self, field: str, message: str | None = None) -> None:
        text = message or f"'{field}' does not refer to a usable item."
        super().__init__(text, details=[{"field": field, "message": text}])


class FieldValidationError(AppError):
    """A 422 found by the service, not by Pydantic, because it needs the stored row
    (e.g. a PATCH sends only ``end_date``, and it is before the saved
    ``start_date``). Same shape as a Pydantic validation error."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_error"
    message = "Request validation failed."

    def __init__(self, field: str, message: str) -> None:
        super().__init__(details=[{"field": field, "message": message}])


# ── Response building ─────────────────────────────────────────────────────────


def _request_id_for(request: Request) -> str:
    """The id set by the request-id middleware.

    Read from ``request.state`` first: for unhandled errors, the middleware has
    already finished (and reset the ContextVar) by the time the 500 handler runs.
    """
    return getattr(request.state, "request_id", None) or get_request_id()


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: ErrorDetails = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = jsonable_encoder(details)
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "request_id": _request_id_for(request)},
        headers=headers,
    )


def _validation_details(errors: Sequence[Any]) -> list[dict[str, str]]:
    """Turn Pydantic errors into ``[{"field": "title", "message": "..."}]``.
    Only the location and message are returned, never the submitted value, so a
    rejected password is not echoed back.
    """
    details: list[dict[str, str]] = []
    for err in errors:
        location = [str(part) for part in err.get("loc", ())]
        # Drop the leading "body" / "query" / "path" / "header" marker.
        if location and location[0] in {"body", "query", "path", "header", "cookie"}:
            location = location[1:]
        details.append({"field": ".".join(location) or "request", "message": err.get("msg", "")})
    return details


_HTTP_STATUS_CODES = {
    status.HTTP_404_NOT_FOUND: ("not_found", "Resource not found."),
    status.HTTP_405_METHOD_NOT_ALLOWED: ("method_not_allowed", "Method not allowed."),
}


# ── Handlers ──────────────────────────────────────────────────────────────────


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    logger.info("%s %s -> %s %s", request.method, request.url.path, exc.status_code, exc.code)
    return error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        headers=exc.headers,
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    # A body that isn't valid JSON is a malformed request (400), not a field error.
    if any(err.get("type") == "json_invalid" for err in errors):
        return error_response(
            request,
            status_code=status.HTTP_400_BAD_REQUEST,
            code="bad_request",
            message="Request body is not valid JSON.",
        )
    return error_response(
        request,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="validation_error",
        message="Request validation failed.",
        details=_validation_details(errors),
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code, message = _HTTP_STATUS_CODES.get(exc.status_code, ("http_error", "Request failed."))
    return error_response(
        request,
        status_code=exc.status_code,
        code=code,
        message=message,
        headers=getattr(exc, "headers", None),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Restore the request id for this log line (see _request_id_for).
    token = set_request_id(_request_id_for(request))
    try:
        logger.error("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    finally:
        reset_request_id(token)
    return error_response(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="internal_error",
        message="An unexpected error occurred.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach all handlers to the FastAPI app (called once in app/main.py)."""
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)


"""Here is a complete, single-page architectural explanation and reference guide for your FastAPI
global error-handling module:

FastAPI Error Handling & Response Architecture Guide
This module establishes a centralized, bulletproof error-handling pipeline. It ensures that every
single error returned by the API—whether a business logic violation, a validation failure, a routing
miss, or an unhandled server bug—adheres to a strict, predictable JSON structure.

1. The Standardized API Error Contract
Every error response generated by this module strictly follows this uniform JSON shape:

JSON
{
  "error": {
    "code": "machine_readable_error_code",
    "message": "Human-readable description for users.",
    "details": [{"field": "...", "message": "..."}] 
  },
  "request_id": "unique_tracing_id_for_logs"
}
Services only raise exceptions: Business logic services never construct raw HTTP responses; they
simply raise custom exception classes, keeping business code clean and decoupled from HTTP delivery
mechanisms.

2. The Exception Hierarchy (AppError)
At the core of the module is AppError, inheriting from Python's built-in Exception. It provides
three key attributes: status_code, code, and message.

Base Class (AppError): Accepts an optional custom message, structured details (like field-level
validation errors), and optional HTTP headers.

Domain Subclasses (4xx Range):

BadRequestError (400)

_UnauthorizedError (401): Automatically injects the WWW-Authenticate: Bearer challenge header.
Subclassed by NotAuthenticatedError, InvalidCredentialsError (uses a generic message to prevent
account enumeration attacks), InvalidTokenError, and TokenReusedError.

PermissionDeniedError (403)

NotFoundError (404): Dynamically formats resource names (e.g., "Project not found.") and prevents
data leakage by returning 404 instead of 403 when checking access constraints.

Conflict Exceptions (409 Range): Handles unique constraint violations (ConflictError), optimistic
locking race conditions (StaleUpdateError, which passes fresh database states so UIs can
auto-reload), archiving restrictions, and active reference checks (e.g., StatusInUseError,
RoleInUseError).

Validation Exceptions (422 Range): Handles semantic validation failures like SearchTooShortError or
InvalidReferenceError.

3. Response Building & Request Tracing Helpers
_request_id_for(request): Pulls the unique tracking ID from request.state (or falls back to context
variables) so every error response can be precisely correlated with server logs.

error_response(...): Packages the error code, message, serialized details (using Pydantic's
jsonable_encoder), and request ID into FastAPI's JSONResponse.

_validation_details(errors): Parses raw Pydantic validation error lists, strips out internal
container roots (body, query, etc.) to keep paths clean (e.g., user.email), and intentionally omits
submitted sensitive values (like passwords) for security.

4. Exception Handlers & Global Wiring
The module intercepts and normalizes four distinct error categories:

app_error_handler (Custom Business Errors): Catches AppError instances, logs basic transaction info
at INFO level, and returns the formatted JSON response.

validation_error_handler (Pydantic Validation Failures): Intercepts RequestValidationError. If the
payload is completely broken JSON, it safely downgrades it to a 400 Bad Request; otherwise, it
returns a 422 Unprocessable Content with clean field-level error details.

http_exception_handler (Starlette Routing Errors): Intercepts native Starlette HTTP exceptions (like
404 Not Found or 405 Method Not Allowed) and wraps them into your custom API format using
_HTTP_STATUS_CODES.

unhandled_exception_handler (500 Server Bugs): Acts as a global safety net. It securely dumps full
stack traces into server logs (logger.exception) while returning a sanitized, generic message ("An
unexpected error occurred.") to the client to protect backend implementation details."""
