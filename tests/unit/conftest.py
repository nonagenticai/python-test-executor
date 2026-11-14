"""Shared fixtures for unit tests."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from src.test_service import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    """Async HTTP client bound to the FastAPI app.

    Defaults an X-Tenant-Id header so existing tests pass through the
    orchestrator-only tenant guard; the guard's own behaviour is asserted
    explicitly in the tenant-guard tests with a no-header client.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-Tenant-Id": "test-tenant"},
    ) as ac:
        yield ac
