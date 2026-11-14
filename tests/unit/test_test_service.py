"""Tests for test_service.py — FastAPI endpoints, subprocess mocking, edge cases."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.test_service import (
    HealthResponse,
    TestRequest,
    TestResponse,
    app,
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-Tenant-Id": "test-tenant"},
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Orchestrator-only tenant guard
# ---------------------------------------------------------------------------
class TestTenantGuard:
    async def _no_header_client(self):
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    async def test_test_code_requires_tenant_header(self):
        async with await self._no_header_client() as ac:
            resp = await ac.post("/execute", json={"code": "print(1)"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "X-Tenant-Id header required"

    async def test_health_exempt_without_header(self):
        async with await self._no_header_client() as ac:
            resp = await ac.get("/health")
        assert resp.status_code == 200

    async def test_root_exempt_without_header(self):
        async with await self._no_header_client() as ac:
            resp = await ac.get("/")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Model validation tests
# ---------------------------------------------------------------------------
class TestModels:
    def test_health_response_fields(self):
        hr = HealthResponse(
            status="healthy",
            timestamp="2025-01-01T00:00:00",
            uptime_seconds=123.4,
            memory_usage_mb=50.0,
        )
        assert hr.status == "healthy"
        assert hr.uptime_seconds == 123.4

    def test_test_request_defaults(self):
        tr = TestRequest(code="print(1)")
        assert tr.timeout == 30

    def test_test_request_custom_timeout(self):
        tr = TestRequest(code="print(1)", timeout=60)
        assert tr.timeout == 60

    def test_test_response_fields(self):
        tr = TestResponse(success=True, output="hello", execution_time=0.5)
        assert tr.success is True
        assert tr.output == "hello"


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------
class TestHealthEndpoint:
    async def test_health_returns_200(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200

    async def test_health_response_structure(self, client):
        resp = await client.get("/health")
        data = resp.json()
        assert data["status"] == "healthy"
        assert "timestamp" in data
        assert "uptime_seconds" in data
        assert "memory_usage_mb" in data

    async def test_health_uptime_positive(self, client):
        resp = await client.get("/health")
        assert resp.json()["uptime_seconds"] >= 0

    async def test_health_memory_positive(self, client):
        resp = await client.get("/health")
        assert resp.json()["memory_usage_mb"] > 0


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------
class TestRootEndpoint:
    async def test_root_returns_200(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200

    async def test_root_response_structure(self, client):
        resp = await client.get("/")
        data = resp.json()
        assert data["service"] == "Python Test Executor"
        assert data["version"] == "1.0.0"
        assert data["status"] == "running"
        assert "/health" in data["endpoints"]
        assert "/execute" in data["endpoints"]


# ---------------------------------------------------------------------------
# POST /execute — success path
# ---------------------------------------------------------------------------
class TestTestCodeSuccess:
    async def test_successful_code_execution(self, client):
        """Test with actual simple code execution — safe print statement."""
        with patch("src.test_service.uuid7", return_value="test-uuid"):
            resp = await client.post(
                "/execute",
                json={"code": "print('hello world')"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "hello world" in data["output"]
        assert data["execution_time"] >= 0
        # canonical executor contract fields
        assert data["exit_code"] == 0
        assert data["test_id"] == "test-uuid"
        assert data["error"] is None

    async def test_code_with_custom_timeout(self, client):
        with patch("src.test_service.uuid7", return_value="test-uuid"):
            resp = await client.post(
                "/execute",
                json={"code": "print('ok')", "timeout": 10},
            )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    async def test_no_output_code(self, client):
        """Code that produces no stdout."""
        with patch("src.test_service.uuid7", return_value="test-uuid"):
            resp = await client.post(
                "/execute",
                json={"code": "x = 1"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["output"] == "No output"


# ---------------------------------------------------------------------------
# POST /execute — syntax error path
# ---------------------------------------------------------------------------
class TestTestCodeSyntaxError:
    async def test_syntax_error_detected(self, client):
        with patch("src.test_service.uuid7", return_value="test-uuid"):
            resp = await client.post(
                "/execute",
                json={"code": "def foo(\n  invalid syntax here!!!"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "syntax error" in data["output"].lower() or "Syntax" in data["output"]


# ---------------------------------------------------------------------------
# POST /execute — runtime error path
# ---------------------------------------------------------------------------
class TestTestCodeRuntimeError:
    async def test_runtime_error(self, client):
        with patch("src.test_service.uuid7", return_value="test-uuid"):
            resp = await client.post(
                "/execute",
                json={"code": "raise ValueError('test error')"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "ValueError" in data["output"] or "test error" in data["output"]


# ---------------------------------------------------------------------------
# POST /execute — timeout path
# ---------------------------------------------------------------------------
class TestTestCodeTimeout:
    async def test_timeout_mocked(self, client):
        """Mock subprocess to simulate timeout."""
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(side_effect=TimeoutError)
        mock_process.kill = MagicMock()
        mock_process.wait = AsyncMock()

        with (
            patch("src.test_service.uuid7", return_value="test-uuid"),
            patch("src.test_service.py_compile.compile"),
            patch("src.test_service.asyncio.create_subprocess_exec", return_value=mock_process),
        ):
            resp = await client.post(
                "/execute",
                json={"code": "import time; time.sleep(100)", "timeout": 1},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "timed out" in data["output"].lower()
        mock_process.kill.assert_called_once()


# ---------------------------------------------------------------------------
# POST /execute — internal error path
# ---------------------------------------------------------------------------
class TestTestCodeInternalError:
    async def test_internal_exception(self, client):
        """Force an exception in the outer try block."""
        with patch("src.test_service.uuid7", side_effect=RuntimeError("boom")):
            resp = await client.post(
                "/execute",
                json={"code": "print(1)"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "internal error" in data["output"].lower()


# ---------------------------------------------------------------------------
# POST /execute — subprocess returncode != 0
# ---------------------------------------------------------------------------
class TestTestCodeNonZeroReturn:
    async def test_nonzero_returncode(self, client):
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(return_value=(b"some error output", None))
        mock_process.returncode = 1

        with (
            patch("src.test_service.uuid7", return_value="test-uuid"),
            patch("src.test_service.py_compile.compile"),
            patch("src.test_service.asyncio.create_subprocess_exec", return_value=mock_process),
        ):
            resp = await client.post(
                "/execute",
                json={"code": "print(1)"},
            )

        data = resp.json()
        assert data["success"] is False
        assert data["output"] == "some error output"


# ---------------------------------------------------------------------------
# POST /execute — stdout is None
# ---------------------------------------------------------------------------
class TestTestCodeNullStdout:
    async def test_null_stdout(self, client):
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(return_value=(None, None))
        mock_process.returncode = 0

        with (
            patch("src.test_service.uuid7", return_value="test-uuid"),
            patch("src.test_service.py_compile.compile"),
            patch("src.test_service.asyncio.create_subprocess_exec", return_value=mock_process),
        ):
            resp = await client.post(
                "/execute",
                json={"code": "x = 1"},
            )

        data = resp.json()
        assert data["output"] == "No output"


# ---------------------------------------------------------------------------
# POST /execute — pytest test_code contract (real pytest subprocess)
# ---------------------------------------------------------------------------
class TestExecuteWithPytest:
    """When test_code is supplied the executor runs code+test_code under pytest;
    a run that collects zero tests is NOT a pass (closes the dropped-test_code hole)."""

    async def test_passing_tests_succeed_with_counts(self, client):
        resp = await client.post(
            "/execute",
            json={
                "code": "def add(a, b):\n    return a + b\n",
                "test_code": "def test_add():\n    assert add(1, 2) == 3\n\ndef test_add_zero():\n    assert add(0, 0) == 0\n",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["exit_code"] == 0
        assert data["tests_passed"] == 2
        assert data["tests_run"] == 2
        assert data["tests_failed"] == 0
        assert data["no_tests_run"] is False
        assert data["error"] is None

    async def test_failing_tests_fail(self, client):
        resp = await client.post(
            "/execute",
            json={
                "code": "def add(a, b):\n    return a + b\n",
                "test_code": "def test_wrong():\n    assert add(1, 1) == 3\n",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["tests_failed"] >= 1
        assert data["error"] == "tests_failed"

    async def test_zero_collected_tests_is_not_a_pass(self, client):
        """test_code with no test_* functions collects zero tests → pytest exit 5 → fail."""
        resp = await client.post(
            "/execute",
            json={
                "code": "def add(a, b):\n    return a + b\n",
                "test_code": "x = add(1, 2)  # no test_* functions, nothing is collected\n",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["no_tests_run"] is True
        assert data["exit_code"] == 5
        assert data["error"] == "no_tests_collected"

    async def test_syntax_error_in_test_code_fails(self, client):
        resp = await client.post(
            "/execute",
            json={
                "code": "def add(a, b):\n    return a + b\n",
                "test_code": "def test_x(:\n    pass\n",  # syntax error
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"] == "syntax_error"

    async def test_code_only_path_is_preserved(self, client):
        """No test_code → legacy script-execution contract (success == exit 0). Regression guard:
        the dropped-test_code fix must NOT flip code-only callers to failure."""
        resp = await client.post(
            "/execute",
            json={"code": "print('ran as a script')"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["no_tests_run"] is False
        assert "ran as a script" in data["output"]


# ---------------------------------------------------------------------------
# Exception handler
# ---------------------------------------------------------------------------
class TestExceptionHandler:
    async def test_unhandled_exception_returns_500(self):
        """Force a 500 via the global exception handler.

        Need raise_server_exceptions=False so httpx doesn't re-raise
        the RuntimeError before we can inspect the response.
        """
        from src.test_service import app as _app

        @_app.get("/test-error-endpoint")
        async def _raise():
            raise RuntimeError("unexpected boom")

        transport = ASGITransport(app=_app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"X-Tenant-Id": "test-tenant"},
        ) as ac:
            resp = await ac.get("/test-error-endpoint")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /execute — missing required field
# ---------------------------------------------------------------------------
class TestTestCodeValidation:
    async def test_missing_code_field(self, client):
        resp = await client.post("/execute", json={})
        assert resp.status_code == 422  # Pydantic validation error

    async def test_empty_code(self, client):
        """Empty string is valid for the model but should produce a syntax-clean file."""
        with patch("src.test_service.uuid7", return_value="test-uuid"):
            resp = await client.post("/execute", json={"code": ""})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Exception handler — HTTPException re-raise (line 153)
# ---------------------------------------------------------------------------
class TestExceptionHandlerHTTPException:
    async def test_http_exception_returns_kag_format(self):
        """HTTPException returns KAG-standard nested error response."""
        import json

        from fastapi import HTTPException as FastAPIHTTPException

        from src.test_service import global_exception_handler

        exc = FastAPIHTTPException(status_code=403, detail="forbidden")
        dummy_request = MagicMock()
        dummy_request.state = MagicMock()
        dummy_request.state.request_id = "test-req-id"

        resp = await global_exception_handler(dummy_request, exc)
        assert resp.status_code == 403
        body = json.loads(resp.body)
        assert body["error"]["code"] == "HTTP_403"
        assert body["error"]["message"] == "forbidden"
        assert body["error"]["request_id"] == "test-req-id"


# ---------------------------------------------------------------------------
# __main__ block (lines 159-171) — import coverage
# ---------------------------------------------------------------------------
class TestMainBlock:
    def test_main_block_runs_uvicorn(self):
        """Cover the if __name__ == '__main__' block by reading and exec-ing the source."""
        import importlib.util
        import os
        import sys as _sys

        spec = importlib.util.find_spec("src.test_service")
        assert spec is not None
        src_path = spec.origin
        assert src_path is not None
        with open(src_path) as f:
            source = f.read()

        # Ensure current_dir is NOT in sys.path so line 163 is exercised
        current_dir = os.path.dirname(os.path.abspath(src_path))
        original_path = _sys.path[:]
        while current_dir in _sys.path:
            _sys.path.remove(current_dir)

        try:
            code = compile(source, src_path, "exec")
            globs: dict = {"__name__": "__main__", "__file__": src_path}
            with patch("uvicorn.run") as mock_run:
                exec(code, globs)  # noqa: S102

            mock_run.assert_called_once_with(
                "test_service:app",
                host="0.0.0.0",
                port=8087,
                log_level="info",
                access_log=True,
            )
        finally:
            _sys.path[:] = original_path
