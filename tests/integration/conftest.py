"""Integration test fixtures for python-test-executor.

These tests hit the REAL service running in a Docker container.
No mocks -- plain HTTP via httpx.

Run with:
    uv run pytest tests/integration/ -v -m integration --no-cov --override-ini="addopts="
"""

import os

import httpx
import pytest

BASE_URL = os.getenv("E2E_BASE_URL", "http://localhost:8087")


@pytest.fixture(scope="session")
def client():
    """Shared httpx client for all integration tests.

    Carries the orchestrator-only X-Tenant-Id header so non-exempt routes
    pass the tenant guard (without it every non-exempt path 401s before
    routing, masking the real 404/405/422 behaviour under test).
    """
    with httpx.Client(
        base_url=BASE_URL,
        timeout=30,
        headers={"X-Tenant-Id": os.getenv("E2E_TENANT_ID", "integration-test")},
    ) as c:
        yield c
