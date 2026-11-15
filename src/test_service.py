#!/usr/bin/env python3
"""Test service module."""

from __future__ import annotations

import asyncio
import logging
import os
import py_compile
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid7

import psutil
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create FastAPI app instance
app = FastAPI(
    title="Python Test Executor",
    description="Executes arbitrary Python code in subprocesses with timeout enforcement and syntax validation. Part of the code-graph-helper system.",
    version="1.0.0",
)


# Orchestrator-only tenant guard: every non-exempt route must carry an
# X-Tenant-Id header (this executor is reachable only from the orchestrator).
# Probe/info paths stay open. This is a lightweight access marker, NOT
# data-tenancy — the executor is stateless and the header is unauthenticated;
# a NetworkPolicy restricting ingress to the orchestrator is the real control.
_TENANT_EXEMPT_PATHS = frozenset({"/health", "/healthz", "/ready", "/"})


@app.middleware("http")
async def require_tenant_header(request: Request, call_next):
    """Reject non-exempt requests that lack an X-Tenant-Id header."""
    if request.url.path not in _TENANT_EXEMPT_PATHS and not request.headers.get("x-tenant-id"):
        return JSONResponse(
            status_code=401,
            content={"detail": "X-Tenant-Id header required"},
        )
    return await call_next(request)


# Request/Response models
class HealthResponse(BaseModel):
    """Health response class."""

    status: str = Field(..., description="Service health status")
    timestamp: str = Field(..., description="Current timestamp")
    uptime_seconds: float = Field(..., description="Service uptime in seconds")
    memory_usage_mb: float = Field(..., description="Memory usage in MB")


class TestRequest(BaseModel):
    """Test request class."""

    code: str = Field(..., description="Python code to test")
    test_code: str | None = Field(
        default=None,
        description="Optional pytest test code. When provided, the code+test_code are run "
        "under pytest and a run that collects zero tests is NOT a pass. When omitted, the "
        "code is executed as a script (success == exit code 0) — the legacy code-only contract.",
    )
    timeout: int = Field(default=30, description="Timeout in seconds")


class TestResponse(BaseModel):
    """Test response class."""

    success: bool = Field(..., description="Whether the test succeeded")
    output: str = Field(..., description="Test output or error message")
    execution_time: float = Field(..., description="Execution time in seconds")
    test_id: str = Field(default="", description="Execution identifier")
    exit_code: int = Field(default=0, description="Process exit code (124 = timeout)")
    error: str | None = Field(default=None, description="Error category, if any")
    tests_run: int = Field(default=0, description="Number of tests collected and executed")
    tests_passed: int = Field(default=0, description="Number of tests that passed")
    tests_failed: int = Field(default=0, description="Number of tests that failed or errored")
    no_tests_run: bool = Field(
        default=False,
        description="True when test_code was provided but pytest collected zero tests",
    )


# pytest exit codes (https://docs.pytest.org/en/stable/reference/exit-codes.html):
#   0 all passed · 1 tests failed · 2 interrupted · 3 internal error
#   4 usage error · 5 NO TESTS COLLECTED
_PYTEST_NO_TESTS_COLLECTED = 5


def _count_before(output: str, suffix: str) -> int:
    """First integer immediately preceding ``suffix`` in ``output``, else 0.

    Linear-time equivalent of ``re.search(r"(\\d+)" + suffix, output)``, which
    CodeQL flagged as py/polynomial-redos: at every start position the greedy
    ``\\d+`` rescans the remainder of a digit run before backtracking, so output
    such as ``"9" * n`` with no suffix costs O(n²). This scans each character a
    constant number of times. ``str.isdecimal()`` preserves Python's ``\\d``
    semantics (Unicode category Nd).
    """
    search_from = 0
    while True:
        end = output.find(suffix, search_from)
        if end == -1:
            return 0
        start = end
        while start > 0 and output[start - 1].isdecimal():
            start -= 1
        if start < end:
            try:
                return int(output[start:end])
            except ValueError:
                # More digits than CPython's int() will convert (default limit:
                # 4300). The documented contract is a 0 fallback; the old regex
                # reached the same int() and let ValueError escape as a 500.
                return 0
        search_from = end + 1


def _parse_pytest_counts(output: str) -> tuple[int, int, int]:
    """Parse (passed, failed, run) from a pytest summary line. Defensive: a count
    that cannot be parsed falls back to 0 — the verdict still follows the exit code."""
    passed = _count_before(output, " passed")
    failed = _count_before(output, " failed")
    # " error" also covers " errors": the old regex's optional ``s`` added no capture.
    errors = _count_before(output, " error")
    failed_total = failed + errors
    return passed, failed_total, passed + failed_total


# Store service start time for uptime calculation
service_start_time = time.time()


# Health check endpoint
@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint to verify service is running."""
    process = psutil.Process()
    memory_info = process.memory_info()

    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(UTC).isoformat(),
        uptime_seconds=time.time() - service_start_time,
        memory_usage_mb=memory_info.rss / 1024 / 1024,
    )


# Root endpoint
@app.get("/")
async def root():
    """Root endpoint with service information."""
    return {
        "service": "Python Test Executor",
        "version": "1.0.0",
        "status": "running",
        "endpoints": ["/health", "/execute", "/docs"],
    }


# Code execution endpoint (canonical executor contract: matches the peer
# go/js/java/rust/csharp/cpp executors and what the orchestrator's
# code_execution_service / http_test_runner POST to).
@app.post("/execute", response_model=TestResponse)
async def execute_code(request: TestRequest):
    """Execute Python code in a safe environment."""
    start_time = time.time()
    test_id = ""

    try:
        temp_id = str(uuid7())
        test_id = temp_id
        has_tests = bool(request.test_code and request.test_code.strip())

        if has_tests:
            # Test contract: run code + test_code under pytest. A clean exit means
            # at least one test was collected and all passed; a run that collects
            # ZERO tests (pytest exit 5) verified nothing and is NOT a pass. This
            # closes the loophole where test_code was silently dropped and any
            # compilable solution was reported as a pass.
            temp_file = Path(f"/tmp/test_submission_{temp_id}.py")
            # Mirror the js executor: the test code shares the submission's module
            # scope, so names defined in `code` are directly visible to the tests.
            temp_file.write_text(f"{request.code}\n\n{request.test_code}")

            # Syntax-check the combined module before running pytest.
            try:
                py_compile.compile(str(temp_file), doraise=True)
            except py_compile.PyCompileError as e:
                temp_file.unlink(missing_ok=True)
                return TestResponse(
                    success=False,
                    output=f"Syntax error: {str(e)}",
                    execution_time=time.time() - start_time,
                    test_id=test_id,
                    exit_code=1,
                    error="syntax_error",
                )

            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pytest",
                str(temp_file),
                "-q",
                "-p",
                "no:cacheprovider",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=request.timeout)
                output = stdout.decode("utf-8") if stdout else "No output"
                exit_code = process.returncode if process.returncode is not None else 0
            except TimeoutError:
                process.kill()
                await process.wait()
                output = f"Execution timed out after {request.timeout} seconds"
                exit_code = 124

            temp_file.unlink(missing_ok=True)

            passed, failed, run = _parse_pytest_counts(output)
            no_tests_run = exit_code == _PYTEST_NO_TESTS_COLLECTED
            success = exit_code == 0
            if success:
                error = None
            elif no_tests_run:
                error = "no_tests_collected"
            elif exit_code == 124:
                error = "timeout"
            else:
                error = "tests_failed"

            return TestResponse(
                success=success,
                output=output,
                execution_time=time.time() - start_time,
                test_id=test_id,
                exit_code=exit_code,
                error=error,
                tests_run=run,
                tests_passed=passed,
                tests_failed=failed,
                no_tests_run=no_tests_run,
            )

        # Legacy code-only contract: execute the code as a script (success == exit 0).
        # Preserved exactly so existing code-only (execute_code) callers do not regress.
        temp_file = Path(f"/tmp/test_code_{temp_id}.py")
        temp_file.write_text(request.code)

        # Compile the code to check for syntax errors
        try:
            py_compile.compile(str(temp_file), doraise=True)
        except py_compile.PyCompileError as e:
            temp_file.unlink(missing_ok=True)
            return TestResponse(
                success=False,
                output=f"Syntax error: {str(e)}",
                execution_time=time.time() - start_time,
                test_id=test_id,
                exit_code=1,
                error="syntax_error",
            )

        # Execute the code with timeout
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(temp_file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=request.timeout)
            output = stdout.decode("utf-8") if stdout else "No output"
            exit_code = process.returncode if process.returncode is not None else 0
            success = exit_code == 0

        except TimeoutError:
            process.kill()
            await process.wait()
            output = f"Execution timed out after {request.timeout} seconds"
            success = False
            exit_code = 124

        # Clean up temporary file
        if temp_file.exists():
            temp_file.unlink()

        return TestResponse(
            success=success,
            output=output,
            execution_time=time.time() - start_time,
            test_id=test_id,
            exit_code=exit_code,
            error=None if success else "execution_failed",
        )

    except Exception as e:
        logger.error(f"Error executing code: {str(e)}")
        return TestResponse(
            success=False,
            output=f"Internal error: {str(e)}",
            execution_time=time.time() - start_time,
            test_id=test_id,
            exit_code=1,
            error="internal_error",
        )


# KAG-standard exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler (KAG format)."""
    import uuid

    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())

    if isinstance(exc, HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": f"HTTP_{exc.status_code}",
                    "message": str(exc.detail),
                    "timestamp": datetime.now(UTC).isoformat(),
                    "request_id": request_id,
                }
            },
        )

    logger.error("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": "An unexpected error occurred",
                "timestamp": datetime.now(UTC).isoformat(),
                "request_id": request_id,
            }
        },
    )


if __name__ == "__main__":
    # Add current directory to Python path for imports
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)

    uvicorn.run(
        "test_service:app",
        host="0.0.0.0",
        port=8087,
        log_level="info",
        access_log=True,
    )
