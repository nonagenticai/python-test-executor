"""Live E2E endpoint tests for python-test-executor.

Hit the REAL service running at http://localhost:8087.
No mocks, no ASGI transport -- plain HTTP via httpx.

Run with:
    uv run pytest tests/integration/test_e2e_endpoints.py -v -m integration --no-cov --override-ini="addopts="
"""

import httpx
import pytest

# ──────────────────────────────────────────────────────────────────────
# Root & Health
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.integration
class TestHealthEndpoint:
    """Tests for GET /health."""

    def test_health_returns_200(self, client: httpx.Client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_status_is_healthy(self, client: httpx.Client):
        resp = client.get("/health")
        data = resp.json()
        assert data["status"] == "healthy"

    def test_health_has_timestamp(self, client: httpx.Client):
        resp = client.get("/health")
        data = resp.json()
        assert "timestamp" in data
        assert isinstance(data["timestamp"], str)
        assert len(data["timestamp"]) > 0

    def test_health_has_uptime(self, client: httpx.Client):
        resp = client.get("/health")
        data = resp.json()
        assert "uptime_seconds" in data
        assert isinstance(data["uptime_seconds"], (int, float))
        assert data["uptime_seconds"] >= 0

    def test_health_has_memory_usage(self, client: httpx.Client):
        resp = client.get("/health")
        data = resp.json()
        assert "memory_usage_mb" in data
        assert isinstance(data["memory_usage_mb"], (int, float))
        assert data["memory_usage_mb"] > 0

    def test_health_response_fields_complete(self, client: httpx.Client):
        resp = client.get("/health")
        data = resp.json()
        expected_fields = {"status", "timestamp", "uptime_seconds", "memory_usage_mb"}
        assert expected_fields.issubset(data.keys())


@pytest.mark.integration
class TestRootEndpoint:
    """Tests for GET /."""

    def test_root_returns_200(self, client: httpx.Client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_root_returns_service_name(self, client: httpx.Client):
        resp = client.get("/")
        data = resp.json()
        assert data["service"] == "Python Test Executor"

    def test_root_returns_version(self, client: httpx.Client):
        resp = client.get("/")
        data = resp.json()
        assert "version" in data
        assert data["version"] == "1.0.0"

    def test_root_returns_status_running(self, client: httpx.Client):
        resp = client.get("/")
        data = resp.json()
        assert data["status"] == "running"

    def test_root_lists_endpoints(self, client: httpx.Client):
        resp = client.get("/")
        data = resp.json()
        assert "endpoints" in data
        assert isinstance(data["endpoints"], list)
        assert "/health" in data["endpoints"]
        assert "/execute" in data["endpoints"]


# ──────────────────────────────────────────────────────────────────────
# OpenAPI / Docs
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.integration
class TestOpenAPIDocs:
    """Tests for auto-generated FastAPI docs."""

    def test_openapi_json_available(self, client: httpx.Client):
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "paths" in data
        assert "/health" in data["paths"]
        assert "/execute" in data["paths"]

    def test_docs_page_available(self, client: httpx.Client):
        resp = client.get("/docs")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


# ──────────────────────────────────────────────────────────────────────
# POST /execute — Happy paths
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.integration
class TestTestCodeHappyPath:
    """Tests for POST /execute with valid inputs."""

    def test_simple_print_statement(self, client: httpx.Client):
        resp = client.post("/execute", json={"code": "print('hello world')"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "hello world" in data["output"]
        assert isinstance(data["execution_time"], (int, float))
        assert data["execution_time"] >= 0

    def test_multiline_code(self, client: httpx.Client):
        code = "x = 1\ny = 2\nprint(x + y)"
        resp = client.post("/execute", json={"code": code})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "3" in data["output"]

    def test_function_definition_and_call(self, client: httpx.Client):
        code = "def greet(name):\n    return f'Hello {name}'\nprint(greet('World'))"
        resp = client.post("/execute", json={"code": code})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "Hello World" in data["output"]

    def test_code_with_imports(self, client: httpx.Client):
        code = "import json\ndata = {'key': 'value'}\nprint(json.dumps(data))"
        resp = client.post("/execute", json={"code": code})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "key" in data["output"]

    def test_code_with_no_output(self, client: httpx.Client):
        """Code that produces no stdout should still succeed."""
        resp = client.post("/execute", json={"code": "x = 42"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_custom_timeout(self, client: httpx.Client):
        resp = client.post("/execute", json={"code": "print('fast')", "timeout": 10})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_response_has_all_fields(self, client: httpx.Client):
        resp = client.post("/execute", json={"code": "print(1)"})
        assert resp.status_code == 200
        data = resp.json()
        expected_fields = {"success", "output", "execution_time"}
        assert expected_fields.issubset(data.keys())


# ──────────────────────────────────────────────────────────────────────
# POST /execute — Syntax errors / failures
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.integration
class TestTestCodeSyntaxErrors:
    """Tests for POST /execute with code that has syntax errors."""

    def test_syntax_error_returns_success_false(self, client: httpx.Client):
        resp = client.post("/execute", json={"code": "def foo(:\n  pass"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "syntax" in data["output"].lower() or "Syntax" in data["output"]

    def test_incomplete_expression(self, client: httpx.Client):
        resp = client.post("/execute", json={"code": "x = 1 +\n"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False

    def test_unclosed_parenthesis(self, client: httpx.Client):
        resp = client.post("/execute", json={"code": "print('hello'"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False

    def test_runtime_error_returns_success_false(self, client: httpx.Client):
        """Code that compiles but raises at runtime should fail."""
        resp = client.post("/execute", json={"code": "raise ValueError('boom')"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "ValueError" in data["output"] or "boom" in data["output"]

    def test_division_by_zero(self, client: httpx.Client):
        resp = client.post("/execute", json={"code": "print(1 / 0)"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "ZeroDivision" in data["output"] or "division" in data["output"].lower()

    def test_undefined_variable(self, client: httpx.Client):
        resp = client.post("/execute", json={"code": "print(undefined_var)"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "NameError" in data["output"] or "undefined" in data["output"].lower()


# ──────────────────────────────────────────────────────────────────────
# POST /execute — Empty / edge cases
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.integration
class TestTestCodeEdgeCases:
    """Tests for POST /execute with edge-case inputs."""

    def test_empty_code_string(self, client: httpx.Client):
        """Empty string is still valid Python (no-op), should succeed."""
        resp = client.post("/execute", json={"code": ""})
        assert resp.status_code == 200
        data = resp.json()
        # Empty code compiles and runs fine (no-op)
        assert data["success"] is True

    def test_whitespace_only_code(self, client: httpx.Client):
        resp = client.post("/execute", json={"code": "   \n\n  "})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_comment_only_code(self, client: httpx.Client):
        resp = client.post("/execute", json={"code": "# just a comment"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_very_long_output(self, client: httpx.Client):
        code = "for i in range(100):\n    print(f'line {i}')"
        resp = client.post("/execute", json={"code": code})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "line 0" in data["output"]
        assert "line 99" in data["output"]


# ──────────────────────────────────────────────────────────────────────
# POST /execute — Validation (422) errors
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.integration
class TestTestCodeValidation:
    """Tests for POST /execute request validation."""

    def test_missing_code_field_returns_422(self, client: httpx.Client):
        resp = client.post("/execute", json={})
        assert resp.status_code == 422
        data = resp.json()
        assert "detail" in data

    def test_missing_body_returns_422(self, client: httpx.Client):
        resp = client.post(
            "/execute",
            content=b"",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422

    def test_invalid_json_body_returns_422(self, client: httpx.Client):
        resp = client.post(
            "/execute",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422

    def test_wrong_type_for_code_returns_422(self, client: httpx.Client):
        resp = client.post("/execute", json={"code": 12345})
        assert resp.status_code == 422

    def test_wrong_type_for_timeout_returns_422(self, client: httpx.Client):
        resp = client.post("/execute", json={"code": "print(1)", "timeout": "abc"})
        assert resp.status_code == 422


# ──────────────────────────────────────────────────────────────────────
# Method not allowed
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.integration
class TestMethodNotAllowed:
    """Tests for wrong HTTP methods on endpoints."""

    def test_get_on_test_code_returns_405(self, client: httpx.Client):
        resp = client.get("/execute")
        assert resp.status_code == 405

    def test_put_on_test_code_returns_405(self, client: httpx.Client):
        resp = client.put("/execute", json={"code": "print(1)"})
        assert resp.status_code == 405

    def test_delete_on_test_code_returns_405(self, client: httpx.Client):
        resp = client.delete("/execute")
        assert resp.status_code == 405

    def test_post_on_health_returns_405(self, client: httpx.Client):
        resp = client.post("/health", json={})
        assert resp.status_code == 405

    def test_patch_on_root_returns_405(self, client: httpx.Client):
        resp = client.patch("/")
        assert resp.status_code == 405


# ──────────────────────────────────────────────────────────────────────
# 404 for unknown routes
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.integration
class TestNotFound:
    """Tests for unknown routes returning 404."""

    def test_unknown_route_returns_404(self, client: httpx.Client):
        resp = client.get("/nonexistent-endpoint")
        assert resp.status_code == 404

    def test_unknown_api_route_returns_404(self, client: httpx.Client):
        resp = client.get("/api/does-not-exist")
        assert resp.status_code == 404

    def test_unknown_nested_route_returns_404(self, client: httpx.Client):
        resp = client.post("/api/v1/execute", json={"code": "print(1)"})
        assert resp.status_code in (404, 405)


# ──────────────────────────────────────────────────────────────────────
# Error response format consistency
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.integration
class TestErrorResponseFormat:
    """Verify error responses are valid JSON."""

    def test_422_is_json(self, client: httpx.Client):
        resp = client.post("/execute", json={})
        assert resp.status_code == 422
        data = resp.json()
        assert "detail" in data

    def test_404_is_json(self, client: httpx.Client):
        resp = client.get("/nonexistent")
        assert resp.status_code == 404
        data = resp.json()
        assert "detail" in data

    def test_405_is_json(self, client: httpx.Client):
        resp = client.get("/execute")
        assert resp.status_code == 405
        # FastAPI returns JSON for 405
        data = resp.json()
        assert "detail" in data
