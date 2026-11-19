"""Offset pagination for every list endpoint (docs/api.md, section 4.1).

In a router::

    @router.get("/projects/{public_id}/teams")
    async def list_teams(page: PageQuery, ...) -> Page[TeamOut]: ...

    # a list with another default page size (spaces: 50)
    async def list_spaces(page: Annotated[PageParams, Depends(page_params(50))], ...)

In a repository::

    statement = select(Team).where(...).order_by(Team.name, Team.id)
    items, pagination = await paginate(session, statement, page)

``paginate`` runs at most two statements:

1. ``SELECT count(*)`` over the filtered statement (without ORDER BY), so
   ``total_items`` counts every match, not just this page;
2. the statement itself with ``LIMIT``/``OFFSET``. It is skipped when the page
   is past the end (nothing to fetch). That also means an absurdly large
   ``page`` never reaches PostgreSQL as a huge OFFSET.

The statement must already be filtered and fully ordered, ending with ``id``
as a tie-breaker (section 4.2); otherwise rows can repeat or vanish between pages.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Query
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.common import Pagination

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class PageParams:
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


def page_params(
    default_page_size: int = DEFAULT_PAGE_SIZE,
) -> Callable[..., PageParams]:
    """Build the FastAPI dependency that reads ``page`` and ``page_size``.

    Out-of-range values (``page=0``, ``page_size=101``, ``page=abc``) are
    rejected by FastAPI with ``422 validation_error`` before the endpoint runs.
    """
    if not 1 <= default_page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"default_page_size must be between 1 and {MAX_PAGE_SIZE}")

    def dependency(
        page: Annotated[int, Query(ge=1, description="Page number, from 1.")] = 1,
        page_size: Annotated[
            int, Query(ge=1, le=MAX_PAGE_SIZE, description="Items per page.")
        ] = default_page_size,
    ) -> PageParams:
        return PageParams(page=page, page_size=page_size)

    return dependency


# The usual case: default page size 20.
PageQuery = Annotated[PageParams, Depends(page_params())]


async def paginate(
    session: AsyncSession,
    statement: Select[Any],
    params: PageParams,
    *,
    scalars: bool = True,
) -> tuple[list[Any], Pagination]:
    """Return one page of ``statement`` and its pagination metadata.

    ``scalars=True`` (default) returns ORM objects, e.g. ``list[Team]``, for a
    statement like ``select(Team)``. Use ``scalars=False`` when the statement
    selects several columns, e.g. ``select(Space, task_count)``: each item is
    then a row that unpacks as ``space, count``.
    """
    # ORDER BY doesn't change a count, so it is dropped from the subquery.
    # Loader options (joinedload) are ignored inside a subquery, so the count
    # doesn't join the tables they would load.
    count_statement = select(func.count()).select_from(
        statement.order_by(None).subquery()
    )
    total_items: int = await session.scalar(count_statement) or 0

    items: list[Any] = []
    if params.offset < total_items:
        page_statement = statement.limit(params.limit).offset(params.offset)
        if scalars:
            items = list(await session.scalars(page_statement))
        else:
            items = list(await session.execute(page_statement))

    pagination = Pagination.build(
        page=params.page, page_size=params.page_size, total_items=total_items
    )
    return items, pagination
