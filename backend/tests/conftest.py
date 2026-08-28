from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

database_file = tempfile.NamedTemporaryFile(prefix="pulse-tests-", suffix=".sqlite3", delete=False)
database_file.close()
os.environ.setdefault("PULSE_ENVIRONMENT", "test")
os.environ.setdefault("PULSE_DATABASE_URL", f"sqlite+aiosqlite:///{database_file.name}")
os.environ.setdefault("PULSE_JOB_RUNNER_ENABLED", "false")
os.environ.setdefault("PULSE_COOKIE_SECURE", "false")
os.environ.setdefault("PULSE_BOOTSTRAP_TOKEN", "test-bootstrap-token")
os.environ.setdefault("PULSE_ALLOWED_HOSTS", '["testserver"]')

from app.db import Base, engine  # noqa: E402
from app.integrations import models as integration_models  # noqa: E402, F401
from app.main import app  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def reset_database() -> AsyncIterator[None]:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture
def owner_payload() -> dict[str, str]:
    return {
        "workspace_name": "Test Workspace",
        "workspace_slug": "test-workspace",
        "email": "owner@example.com",
        "full_name": "Owner User",
        "password": "correct horse battery staple",
    }


@pytest_asyncio.fixture
async def owner_auth(client: httpx.AsyncClient, owner_payload: dict[str, str]) -> dict[str, object]:
    response = await client.post(
        "/api/v1/auth/bootstrap",
        headers={"X-Bootstrap-Token": "test-bootstrap-token"},
        json=owner_payload,
    )
    assert response.status_code == 201, response.text
    return response.json()
