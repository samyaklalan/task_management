"""Pagination and the shared response shapes (B21: app/schemas/common.py,
app/utils/pagination.py), checked against docs/api.md sections 3 and 4.1."""

from collections.abc import AsyncIterator
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import joinedload

from app.core.exceptions import NotFoundError, StaleUpdateError, register_exception_handlers
from app.models import Task
from app.schemas.common import ErrorDetail, ErrorResponse, Page, Pagination
from app.utils.pagination import PageParams, PageQuery, page_params, paginate
from tests.factories import make_project, make_space, make_task, make_task_status
from tests.utils.query_counter import count_queries

# ── Pagination.build ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("total_items", "page_size", "total_pages"),
    [
        (0, 20, 0),
        (1, 20, 1),
        (20, 20, 1),
        (21, 20, 2),
        (250, 20, 13),
        (5, 1, 5),
        (10**18 + 1, 100, 10**16 + 1),
    ],
)
def test_total_pages(total_items: int, page_size: int, total_pages: int) -> None:
    pagination = Pagination.build(page=1, page_size=page_size, total_items=total_items)
    assert pagination.total_pages == total_pages


def test_page_serializes_to_the_documented_shape() -> None:
    page = Page[ErrorDetail](
        items=[ErrorDetail(field="title", message="Too short")],
        pagination=Pagination.build(page=1, page_size=20, total_items=1),
    )
    assert page.model_dump() == {
        "items": [{"field": "title", "message": "Too short"}],
        "pagination": {"page": 1, "page_size": 20, "total_items": 1, "total_pages": 1},
    }


def test_page_params_offset_and_limit() -> None:
    assert PageParams(page=1, page_size=20).offset == 0
    assert PageParams(page=3, page_size=20).offset == 40  # skips pages 1 and 2
    assert PageParams(page=3, page_size=20).limit == 20


@pytest.mark.parametrize("bad_default", [0, 101])
def test_page_params_rejects_an_invalid_default(bad_default: int) -> None:
    with pytest.raises(ValueError, match="default_page_size"):
        page_params(bad_default)


# ── Query parameters through a real FastAPI app ───────────────────────────────


def _build_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/default")
    async def default(page: PageQuery) -> dict[str, int]:
        return {"page": page.page, "page_size": page.page_size}

    @app.get("/fifty")
    async def fifty(page: Annotated[PageParams, Depends(page_params(50))]) -> dict[str, int]:
        return {"page": page.page, "page_size": page.page_size}

    @app.get("/missing")
    async def missing() -> None:
        raise NotFoundError("Project")

    @app.get("/stale")
    async def stale() -> None:
        raise StaleUpdateError(current={"title": "Newer title"})

    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("/default", {"page": 1, "page_size": 20}),
        ("/default?page=3&page_size=100", {"page": 3, "page_size": 100}),
        ("/fifty", {"page": 1, "page_size": 50}),
        ("/fifty?page_size=5", {"page": 1, "page_size": 5}),
    ],
)
async def test_page_query_parameters(
    client: httpx.AsyncClient, url: str, expected: dict[str, int]
) -> None:
    response = await client.get(url)
    assert response.status_code == 200
    assert response.json() == expected


@pytest.mark.parametrize(
    ("query", "field"),
    [
        ("page=0", "page"),
        ("page=abc", "page"),
        ("page_size=0", "page_size"),
        ("page_size=101", "page_size"),
    ],
)
async def test_out_of_range_page_parameters_are_a_validation_error(
    client: httpx.AsyncClient, query: str, field: str
) -> None:
    response = await client.get(f"/default?{query}")

    assert response.status_code == 422
    body = ErrorResponse.model_validate(response.json())  # the documented error shape
    assert body.error.code == "validation_error"
    assert [detail.field for detail in body.error.details] == [field]


async def test_error_schema_matches_what_the_handlers_send(client: httpx.AsyncClient) -> None:
    missing = await client.get("/missing")
    stale = await client.get("/stale")

    assert missing.status_code == 404
    assert ErrorResponse.model_validate(missing.json()).error.code == "not_found"
    assert "details" not in missing.json()["error"]  # left out when there's nothing to add
    assert stale.status_code == 409
    stale_body = ErrorResponse.model_validate(stale.json())
    assert stale_body.error.details == {"current": {"title": "Newer title"}}


# ── paginate() against PostgreSQL ─────────────────────────────────────────────


@pytest.fixture
async def project_with_25_tasks(db_session: AsyncSession) -> int:
    """25 tasks numbered 1-25: 10 in one status, 15 in another."""
    project = await make_project(db_session)
    space = await make_space(db_session, project=project)
    todo = await make_task_status(db_session, project=project, name="To do")
    done = await make_task_status(db_session, project=project, name="Done", category="done")
    for number in range(1, 26):
        status = todo if number <= 10 else done
        await make_task(db_session, project=project, space=space, status=status)
    return project.id


def _tasks_by_number(project_id: int) -> Select[tuple[Task]]:
    return (
        select(Task)
        .where(Task.project_id == project_id)
        .options(joinedload(Task.status))
        .order_by(Task.number, Task.id)
    )


async def test_middle_page(
    engine: AsyncEngine, db_session: AsyncSession, project_with_25_tasks: int
) -> None:
    statement = _tasks_by_number(project_with_25_tasks)

    with count_queries(engine) as queries:
        items, pagination = await paginate(db_session, statement, PageParams(2, 10))

    assert [task.number for task in items] == list(range(11, 21))
    assert items[0].status.name  # loaded by joinedload, no lazy load
    assert pagination == Pagination(page=2, page_size=10, total_items=25, total_pages=3)
    assert queries.count == 2, str(queries)  # one COUNT, one page
    count_sql, page_sql = queries.statements
    assert "count(" in count_sql.lower() and "JOIN" not in count_sql  # joinedload skipped
    assert "ORDER BY" not in count_sql
    assert "JOIN" in page_sql and "LIMIT" in page_sql


async def test_last_partial_page(db_session: AsyncSession, project_with_25_tasks: int) -> None:
    items, pagination = await paginate(
        db_session, _tasks_by_number(project_with_25_tasks), PageParams(3, 10)
    )
    assert [task.number for task in items] == [21, 22, 23, 24, 25]
    assert pagination.total_pages == 3


async def test_page_past_the_end_is_empty_and_skips_the_page_query(
    engine: AsyncEngine, db_session: AsyncSession, project_with_25_tasks: int
) -> None:
    with count_queries(engine) as queries:
        items, pagination = await paginate(
            db_session, _tasks_by_number(project_with_25_tasks), PageParams(10**9, 100)
        )

    assert items == []
    assert pagination == Pagination(page=10**9, page_size=100, total_items=25, total_pages=1)
    assert queries.count == 1, str(queries)  # only the COUNT


async def test_no_matches(db_session: AsyncSession) -> None:
    items, pagination = await paginate(db_session, _tasks_by_number(-1), PageParams())
    assert items == []
    assert pagination == Pagination(page=1, page_size=20, total_items=0, total_pages=0)


async def test_rows_with_several_columns(
    db_session: AsyncSession, project_with_25_tasks: int
) -> None:
    statement = (
        select(Task.status_id, func.count().label("task_count"))
        .where(Task.project_id == project_with_25_tasks)
        .group_by(Task.status_id)
        .order_by(Task.status_id)
    )

    rows, pagination = await paginate(db_session, statement, PageParams(), scalars=False)

    assert sorted(task_count for _status_id, task_count in rows) == [10, 15]
    assert pagination.total_items == 2  # counts the groups, not the tasks
