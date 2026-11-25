"""Response shapes shared by every endpoint (docs/api.md, sections 3 and 4.1).

* ``ErrorResponse``: the body of every error. The handlers in
  app/core/exceptions.py build it; these classes describe it in the OpenAPI
  docs (``responses={404: {"model": ErrorResponse}}``) and let tests check that
  the handlers really produce this shape.
* ``Page[T]``: every paginated list, e.g. ``Page[TaskOut]``.
* ``SortOrder``: the ``order`` query parameter of every sortable list.
* ``RequestModel``: base class of every request body.
* ``RoleRef``: a role as other responses show it, ``{"id": 3, "name": "Manager"}``.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SortOrder = Literal["asc", "desc"]

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

# Shared by organizations, projects, teams…: max 2000 characters, blank → null.
DESCRIPTION_MAX_LENGTH = 2000
Description = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=DESCRIPTION_MAX_LENGTH),
    AfterValidator(lambda text: text or None),
]


class RequestModel(BaseModel):
    """Base of every request body: an unknown field is a 422, not silently ignored,
    so a typo like ``{"nmae": "x"}`` is reported instead of doing nothing."""

    model_config = ConfigDict(extra="forbid")


# Yes. extra="forbid" means the client cannot send
#  extra fields. Any field the schema doesn't declare causes a 422 error.


class RoleRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


# RoleRef = {"id": 3, "name": "Manager"}


# It tells you which role a user has (id and name only), not what the role can do.
# It keeps API responses small and gives every role the same shape.
class UserRef(BaseModel):
    """A user as other items show them (member lists, assignees): id and name
    only. Emails are only listed by USR-3, for callers with ``users.view``."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


# ── Errors ────────────────────────────────────────────────────────────────────


class ErrorDetail(BaseModel):
    """One problem with one field, e.g. ``{"field": "title", "message": "..."}``."""

    field: str
    message: str


class ErrorBody(BaseModel):
    code: str = Field(description="Stable, machine-readable; the frontend switches on it.")
    message: str = Field(description="Human-readable; may change.")
    details: list[ErrorDetail] | dict[str, Any] | None = Field(
        default=None,
        description=(
            "Per-field problems (validation, conflict, invalid reference) or extra "
            "data for one error code, e.g. the current version for stale_update. "
            "Left out of the response when there is nothing to add."
        ),
    )


class ErrorResponse(BaseModel):
    error: ErrorBody
    request_id: str = Field(description="Matches the server log line for this request.")


# ── Pagination ────────────────────────────────────────────────────────────────


class Pagination(BaseModel):
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total_items: int = Field(ge=0, description="All matches, not just this page.")
    total_pages: int = Field(ge=0, description="0 when there are no matches.")

    @classmethod
    def build(cls, *, page: int, page_size: int, total_items: int) -> "Pagination":
        return cls(
            page=page,
            page_size=page_size,
            total_items=total_items,
            # Whole-number ceiling division: exact for any size (no float rounding).
            total_pages=-(-total_items // page_size),
        )


class Page[T](BaseModel):
    """A page of a list. ``items`` is empty when ``page`` is past the last page."""

    items: list[T]
    pagination: Pagination
