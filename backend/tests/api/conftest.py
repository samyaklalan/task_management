"""Fixtures for API tests: the real app, talking to the test database.

Every request in a test uses the test's own ``db_session`` (tests/conftest.py),
so all rows a request creates are rolled back when the test ends, even though
the services call ``commit()``.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.main import create_app


@pytest.fixture
def app(db_session: AsyncSession) -> FastAPI:
    application = create_app()

    async def test_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = test_session
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1") as http:
        yield http
