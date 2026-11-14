# python-test-executor

Executes arbitrary Python code and analyzes it for quality issues. Part of the **code-graph-helper** microservices system, this service receives Python source code via HTTP, runs it in a subprocess with a configurable timeout, and returns the output. It also includes a result processor that parses output from static analysis tools (ruff, ty, ruff format, bandit, eslint, prettier, vulture, radon) into structured quality issue reports.

**Port:** 8087

## Architecture

```
                          ┌─────────────────────────┐
  POST /execute ──────► │   FastAPI (test_service) │
                          │   - write code to /tmp   │
                          │   - py_compile check     │
                          │   - subprocess exec      │
                          │   - timeout + cleanup    │
                          └─────────────────────────┘

  ResultProcessor (library, not exposed via API)
    - parses stdout/stderr from: ruff, ty, ruff-format,
      bandit, eslint, vulture, radon, prettier
    - returns list[QualityServiceIssue]
```

**Key components:**

| File | Purpose |
|------|---------|
| `src/test_service.py` | FastAPI app — `/health`, `/execute`, `/` endpoints |
| `src/result_processor.py` | Parses output from 9 quality/linting tools into `QualityServiceIssue` objects |
| `src/quality_models.py` | Pydantic model for `QualityServiceIssue` (tool, severity, message, line, column, rule_id, fixable) |

## API

### `GET /`

Service info and available endpoints.

**Response:**
```json
{
  "service": "Python Test Executor",
  "version": "1.0.0",
  "status": "running",
  "endpoints": ["/health", "/execute", "/docs"]
}
```

### `GET /health`

Health check with uptime and memory usage.

**Response:**
```json
{
  "status": "healthy",
  "timestamp": "2026-03-10T12:00:00+00:00",
  "uptime_seconds": 3600.5,
  "memory_usage_mb": 42.1
}
```

### `POST /execute`

Execute Python code and return the result.

**Request:**
```json
{
  "code": "print('hello world')",
  "timeout": 30
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `code` | string | required | Python source code to execute |
| `timeout` | int | 30 | Max execution time in seconds |

**Response:** (canonical executor contract — matches the peer go/js/java/rust/csharp/cpp executors)
```json
{
  "success": true,
  "output": "hello world\n",
  "execution_time": 0.05,
  "test_id": "0193...",
  "exit_code": 0,
  "error": null
}
```

`exit_code` is the subprocess return code (`124` on timeout); `error` is a short failure
category (`syntax_error` / `execution_failed` / `internal_error`) or `null` on success.

**Execution flow:**
1. Writes code to a temp file (`/tmp/test_code_<uuid7>.py`)
2. Compiles with `py_compile` to catch syntax errors early
3. Runs as a subprocess (`sys.executable <tempfile>`) with the configured timeout
4. Kills the process on timeout
5. Cleans up the temp file
6. Returns success/failure, stdout, and wall-clock execution time

### `GET /docs`

Auto-generated OpenAPI (Swagger) docs.

## ResultProcessor (Library Component)

Not exposed via HTTP. Used internally (or by importing services) to parse tool output into structured issues.

**Supported tools:**

| Tool | Input format | What it detects |
|------|-------------|-----------------|
| ruff | JSON (`ruff check --output-format json`) | Lint + import-sort (`I`) issues; exit >= 2 or no JSON = `tool-failure` |
| ty | JSON (`ty check --output-format gitlab`) | Type errors (unresolved imports summarised); exit >= 2 or no JSON = `tool-failure` |
| ruff-format | Text (`ruff format --check`) | Formatting issues; exit 2 = `tool-failure` |
| bandit | JSON (`-f json`) | Security vulnerabilities |
| eslint | JSON | JS/TS lint issues |
| vulture | Text | Dead/unused code |
| radon | JSON (`-j`) | Cyclomatic complexity > 10 |
| prettier | Text | Formatting issues |

`parse_tool_output` requires `exit_code`: ruff / ruff-format / ty exit >= 2 is reported as a failure, never as clean or as findings (ty can exit 2 while printing valid JSON).

Each issue is a `QualityServiceIssue`:
```python
class QualityServiceIssue(BaseModel):
    tool: str
    severity: str        # error, warning, info
    message: str
    line: int | None
    column: int | None
    rule_id: str | None
    fixable: bool
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | 8087 | HTTP listen port |
| `TEST_MODE` | `true` | Set in Docker; not currently used by app logic |
| `PYTHONPATH` | `/app:/app/src` | Set in Docker to resolve imports |

No database. No Redis connection at runtime (redis is a dependency but not used by the current endpoints).

## Docker

```bash
docker compose up -d          # start on port 8087
docker compose down -v        # stop
```

The container image ships pytest + hypothesis (from `uv.lock`) and the analysis CLIs ruff 0.17.3, ty 0.0.92 and bandit 1.9.4 (isolated `uv tool` installs, outside the locked venv). This makes them available for code executed inside the container.

The Docker socket is mounted read-only (`/var/run/docker.sock:/var/run/docker.sock:ro`) for potential container-in-container use.

## Dependencies on Other Services

- **coding-testing** (port 8689) — the testing orchestrator that delegates Python execution to this service
- **coding-gateway** (port 8687) — routes external requests through to coding-testing

No outbound calls to other services from within this codebase.

## Development

```bash
uv sync --dev                       # install deps
uv run ruff check .                 # lint
uv run ruff format --check .        # format check
uv run pytest                       # run tests
uv run uvicorn src.test_service:app --port 8087  # run locally
```

## CI/CD

GitHub Actions on `nonagenticai/python-test-executor`, via the public `ci-workflows` archetype on GitHub-hosted runners. On every PR and on `main`:
1. Secret scan and private-infrastructure scan
2. ruff (check + format), ty, the pytest unit suite, `uv build`, bandit and pip-audit
3. Image build and a `/health` smoke against it
4. From `main`: push `sha-<commit>` + `latest` to `ghcr.io/nonagenticai/python-test-executor` with a SLSA provenance attestation

There is no deploy job: this service is retired from the estate's cluster, and the repository is published so its tests, image build and job contract stay reproducible.

## Known Issues

- **Unused redis dependency** — redis is listed as a dependency but not imported or used by any endpoint. Left in place for potential future use by the result processor.
