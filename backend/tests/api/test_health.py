"""The first runnable API: app/main.py, the request id, and SYS-1 GET /health.

Requests go through the real application (``create_app()``) in memory, with
httpx's ASGI transport: no server, no network. The health check is pointed at
the TEST database, never the development one.
"""

from collections.abc import AsyncIterator

import anyio
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.v1 import health as health_module
from app.core.config import get_settings
from app.db import session as db_session
from app.main import create_app

HEALTH_URL = "/api/v1/health"


@pytest.fixture
def app(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    # The app's database engine is the test engine for these tests.
    monkeypatch.setattr(db_session, "_engine", engine)
    monkeypatch.setattr(db_session, "_session_factory", db_session.create_session_factory(engine))
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    # raise_app_exceptions=False: an unhandled error becomes the 500 response a
    # real server sends, instead of being re-raised inside the test.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


# ── SYS-1 GET /health ─────────────────────────────────────────────────────────


async def test_health_ok(client: httpx.AsyncClient) -> None:
    response = await client.get(HEALTH_URL)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
    assert response.headers["cache-control"] == "no-store"


async def test_health_reports_database_down(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def database_down() -> bool:
        return False

    monkeypatch.setattr(db_session, "ping_database", database_down)

    response = await client.get(HEALTH_URL)

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert response.headers["cache-control"] == "no-store"


async def test_health_answers_even_when_the_database_hangs(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def database_hangs() -> bool:
        await anyio.sleep(60)
        return True

    monkeypatch.setattr(db_session, "ping_database", database_hangs)
    monkeypatch.setattr(health_module, "HEALTH_TIMEOUT_SECONDS", 0.2)

    with anyio.fail_after(5):  # the test itself must not hang either
        response = await client.get(HEALTH_URL)

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


async def test_health_against_an_unreachable_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real connection failure (nothing listens on port 1), not a stub."""
    unreachable = db_session.create_engine(
        url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/nowhere_test"
    )
    monkeypatch.setattr(db_session, "_engine", unreachable)
    monkeypatch.setattr(
        db_session, "_session_factory", db_session.create_session_factory(unreachable)
    )
    transport = httpx.ASGITransport(app=create_app())
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get(HEALTH_URL)
    finally:
        await unreachable.dispose()

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


async def test_post_to_health_is_method_not_allowed(client: httpx.AsyncClient) -> None:
    response = await client.post(HEALTH_URL)

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
    assert response.headers["allow"] == "GET"


# ── Request id ────────────────────────────────────────────────────────────────


async def test_every_response_has_a_request_id(client: httpx.AsyncClient) -> None:
    first = await client.get(HEALTH_URL)
    second = await client.get(HEALTH_URL)

    for response in (first, second):
        assert len(response.headers["x-request-id"]) == 16
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


async def test_error_body_and_header_carry_the_same_request_id(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/v1/no-such-endpoint")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["request_id"] == response.headers["x-request-id"]


async def test_a_valid_incoming_request_id_is_reused(client: httpx.AsyncClient) -> None:
    response = await client.get(HEALTH_URL, headers={"X-Request-ID": "bff-0f6c2a1e9b7d"})

    assert response.headers["x-request-id"] == "bff-0f6c2a1e9b7d"


@pytest.mark.parametrize(
    "incoming",
    ["short", "x" * 65, "has spaces in it", "evil\tinjected-log-line", "semi;colon;value"],
)
async def test_an_unsafe_incoming_request_id_is_replaced(
    client: httpx.AsyncClient, incoming: str
) -> None:
    response = await client.get(HEALTH_URL, headers={"X-Request-ID": incoming})

    assert response.headers["x-request-id"] != incoming
    assert len(response.headers["x-request-id"]) == 16


async def test_unexpected_error_is_a_500_with_the_request_id(
    app: FastAPI, client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken() -> None:
        raise RuntimeError("secret internal detail")

    app.add_api_route("/api/v1/broken", broken)

    response = await client.get("/api/v1/broken")

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == {"code": "internal_error", "message": "An unexpected error occurred."}
    assert "secret internal detail" not in response.text  # details only in the log
    # The log line for this error carries the same id as the response body.
    error_records = [r for r in caplog.records if r.getMessage().startswith("Unhandled error")]
    assert [r.request_id for r in error_records] == [body["request_id"]]


# ── App setup ─────────────────────────────────────────────────────────────────


async def test_openapi_lists_the_versioned_health_endpoint(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/openapi.json")

    assert response.status_code == 200
    assert HEALTH_URL in response.json()["paths"]


def test_api_docs_are_hidden_in_production() -> None:
    production = get_settings().model_copy(update={"app_env": "production"})
    app = create_app(production)

    assert app.openapi_url is None and app.docs_url is None
