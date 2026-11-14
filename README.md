# python-test-executor

[![CI](https://github.com/nonagenticai/python-test-executor/actions/workflows/ci.yml/badge.svg)](https://github.com/nonagenticai/python-test-executor/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Executes Python code over HTTP and, when test code is supplied, runs it under pytest. Part of
the **code-graph-helper** microservices system, it receives Python source via HTTP, runs it in a
subprocess with a configurable timeout, and returns the captured output and exit code. It also
contains a library component that parses output from eight static-analysis tools into structured
quality-issue objects.

| | |
|---|---|
| **Runtime** | Python 3.14 (`python:3.14-slim`; `pyproject.toml` requires `>=3.14`) |
| **Depends on** | Nothing at runtime: no database, no Redis connection, no outbound service calls |
| **Runs on** | A container listening on `8087`. CI has no deploy job; the service is retired from the estate cluster. |

## Quick start

```bash
git clone https://github.com/nonagenticai/python-test-executor.git
cd python-test-executor
docker compose up -d
curl http://localhost:8087/health
```

## Usage

`/` and `/health` are open. Every other route — including `/execute`, `/docs`, `/redoc` and
`/openapi.json` — requires `X-Tenant-Id`, the access marker the orchestrator injects; without it
the service answers `401 {"detail":"X-Tenant-Id header required"}`. This is a lightweight
marker, **not** authentication and not data tenancy — the real isolation control is the cluster
NetworkPolicy that admits only the orchestrator.

```bash
curl -X POST http://localhost:8087/execute \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-Id: coding-testing' \
  -d '{"code": "print(\"hello world\")", "timeout": 30}'
```

```json
{
  "success": true,
  "output": "hello world\n",
  "execution_time": 0.05,
  "test_id": "0193...",
  "exit_code": 0,
  "error": null,
  "tests_run": 0,
  "tests_passed": 0,
  "tests_failed": 0,
  "no_tests_run": false
}
```

## API

### `GET /`

Service info and the endpoint list. No header required.

```json
{
  "service": "Python Test Executor",
  "version": "1.0.0",
  "status": "running",
  "endpoints": ["/health", "/execute", "/docs"]
}
```

### `GET /health`

Health check with uptime and memory usage. No header required.

```json
{
  "status": "healthy",
  "timestamp": "2026-03-10T12:00:00+00:00",
  "uptime_seconds": 3600.5,
  "memory_usage_mb": 42.1
}
```

### `POST /execute`

Execute Python code. Requires `X-Tenant-Id`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `code` | string | required | Python source code |
| `test_code` | string \| null | `null` | Optional pytest tests. When supplied, `code` and `test_code` are concatenated into one module and run under pytest; a run that collects zero tests is **not** a pass. When omitted, `code` is executed as a plain script (success = exit code 0). |
| `timeout` | int | 30 | Max execution time in seconds |

**Response** (canonical executor contract — matches the peer go/js/java/rust/csharp/cpp
executors):

| Field | Meaning |
|-------|---------|
| `success` | Exit code 0 (and, in pytest mode, at least one test collected) |
| `output` | Combined stdout/stderr, or the timeout/syntax message |
| `execution_time` | Wall-clock seconds (float) |
| `test_id` | uuid7 of this execution |
| `exit_code` | Subprocess return code; `124` on timeout |
| `error` | `null` on success, else `syntax_error`, `timeout`, `no_tests_collected`, `tests_failed`, `execution_failed` or `internal_error` |
| `tests_run` / `tests_passed` / `tests_failed` | Parsed from the pytest summary (0 in script mode) |
| `no_tests_run` | True when `test_code` was provided but pytest collected zero tests |

**Script mode (no `test_code`):**

1. Writes the code to `/tmp/test_code_<uuid7>.py`
2. Compiles it with `py_compile` to catch syntax errors early
3. Runs `sys.executable <tempfile>`
4. Kills the process and returns exit code `124` on timeout
5. Deletes the temp file

**Pytest mode (`test_code` supplied):**

1. Writes `code` + `test_code` to `/tmp/test_submission_<uuid7>.py`
2. Compiles the combined module with `py_compile`
3. Runs `sys.executable -m pytest <file> -q -p no:cacheprovider`
4. Maps pytest exit code `5` (no tests collected) to `error: "no_tests_collected"`,
   any other non-zero code to `"tests_failed"`, and a timeout to `"timeout"` / `124`
5. Deletes the temp file

A syntax error returns HTTP 200 with `success: false`, `exit_code: 1` and
`error: "syntax_error"`; an unexpected exception returns `error: "internal_error"`.

### `GET /docs`, `/redoc`, `/openapi.json`

FastAPI's generated documentation and schema. These are **not** exempt from the tenant header,
so open them with `-H 'X-Tenant-Id: …'` (for example through a browser extension or `curl`).

## ResultProcessor (library component)

Not exposed via HTTP. Parses tool output into `QualityServiceIssue` objects. It handles
**eight** tools:

| Tool | Input format | What it detects |
|------|-------------|-----------------|
| ruff | JSON (`ruff check --output-format json`) | Lint + import-sort (`I`) issues; exit >= 2 or no JSON = `tool-failure` |
| ty | JSON (`ty check --output-format gitlab`) | Type errors (unresolved imports summarised); exit >= 2 or no JSON = `tool-failure` |
| ruff-format | Text (`ruff format --check`) | Formatting issues; exit 2 = `tool-failure` |
| bandit | JSON (`-f json`) | Security vulnerabilities |
| eslint | JSON | JS/TS lint issues |
| prettier | Text | Formatting issues |
| vulture | Text | Dead/unused code |
| radon | JSON (`-j`) | Cyclomatic complexity > 10 |

`parse_tool_output` requires `exit_code`: ruff / ruff-format / ty exit >= 2 is reported as a
failure, never as clean or as findings (ty can exit 2 while printing valid JSON).

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

## Architecture

```
                          ┌─────────────────────────┐
  POST /execute ──────► │   FastAPI (test_service) │
                          │   - X-Tenant-Id guard    │
                          │   - write code to /tmp   │
                          │   - py_compile check     │
                          │   - subprocess exec      │
                          │     (script or pytest)   │
                          │   - timeout + cleanup    │
                          └─────────────────────────┘

  ResultProcessor (library, not exposed via API)
    - parses stdout/stderr from: ruff, ty, bandit, eslint,
      ruff-format, prettier, vulture, radon
    - returns list[QualityServiceIssue]
```

**Key components:**

| File | Purpose |
|------|---------|
| `src/test_service.py` | FastAPI app — `/`, `/health`, `/execute`, tenant guard, script/pytest execution |
| `src/result_processor.py` | Parses output from 8 quality/linting tools into `QualityServiceIssue` objects |
| `src/quality_models.py` | Pydantic model for `QualityServiceIssue` (tool, severity, message, line, column, rule_id, fixable) |

## Configuration

The application reads **no** environment variables: there is no `os.getenv` or `os.environ`
call in `src/`. The listen port is fixed by the uvicorn command line — `--port 8087` in the
Dockerfile `CMD`, and `uvicorn.run(port=8087)` in the `__main__` block.

These variables are set by the image/compose file but are not read by application code:

| Variable | Default | Meaning |
|----------|---------|---------|
| `PORT` | `8087` | Set by `docker-compose.yml`; **not read** — the port comes from the uvicorn CLI |
| `TEST_MODE` | `true` | Set in Docker/compose; not used by app logic |
| `PYTHONPATH` | `/app:/app/src` | Set in the image so `test_service:app` resolves; read by Python's import system, not by app code |
| `PYTHONDONTWRITEBYTECODE` / `PYTHONUNBUFFERED` | `1` | Set in the image for container-friendly execution |

`X-Tenant-Id` is a request header, not an environment variable; see **Usage**.

No database. Redis is declared as a dependency but is not imported by any endpoint (see
**Known Issues**).

## Docker

```bash
docker compose up -d          # start on port 8087
docker compose down -v        # stop
```

The image is `python:3.14-slim` with `uv` 0.13.7 and installs exactly what `uv.lock` pins
(`uv sync --frozen --no-dev`). pytest + hypothesis are runtime dependencies because
`/execute` shells out to `sys.executable -m pytest`. The analysis CLIs ruff 0.17.3, ty 0.0.92
and bandit 1.9.4 are installed with `uv tool install` into their own venvs outside the locked
venv, so they are available to code executed in the container without disturbing the lock.

The Docker socket is mounted read-only (`/var/run/docker.sock:/var/run/docker.sock:ro`) in
`docker-compose.yml` for potential container-in-container use.

## Dependencies

- **coding-testing** (port 8689) — the testing orchestrator that delegates Python execution to
  this service
- **coding-gateway** (port 8687) — routes external requests through to coding-testing

No outbound calls to other services from within this codebase (verified: `src/` imports no
HTTP or Redis client).

## Development

```bash
uv sync --dev                       # install deps
uv run ruff check .                 # lint
uv run ruff format --check .        # format check
uv run ty check src                 # type check
uv run pytest                       # unit tests (integration deselected by addopts)
# integration tests hit a REAL service at http://localhost:8087 (start it first):
uv run pytest tests/integration/ -v -m integration --no-cov --override-ini="addopts="
uv run uvicorn src.test_service:app --port 8087  # run locally
```

## Security

Report vulnerabilities privately through the repository's Security tab or the organisation
policy at [`SECURITY.md`](https://github.com/nonagenticai/.github/blob/main/SECURITY.md). Do not
open a public issue for a security problem.

The `X-Tenant-Id` check is an access marker that keeps accidental callers out; it is not
authentication, and it does not verify a tenant. Do not rely on it for isolation — restrict
network reachability with a NetworkPolicy. Submitted code is executed in the same container as
the service, so the container and its network policy are the security boundary.

## Contributing

Issues and pull requests are welcome. See
[`CONTRIBUTING.md`](https://github.com/nonagenticai/.github/blob/main/CONTRIBUTING.md) for the
organisation-wide rules; this repository's own commands are in **Development** above.

## CI/CD

GitHub Actions on `nonagenticai/python-test-executor`, via the public `ci-workflows` archetype
on GitHub-hosted runners. On every PR and on `main`:

1. Secret scan and private-infrastructure scan
2. `uv lock --check`, dependency install, ruff (check + format), ty, the pytest unit suite,
   bandit and pip-audit
3. Image build and a `/health` smoke test against it
4. From `main`: push `sha-<commit>` + `latest` to
   `ghcr.io/nonagenticai/python-test-executor` with a SLSA provenance attestation

There is no deploy job: this service is retired from the estate's cluster, and the repository is
published so its tests, image build and job contract stay reproducible.

## Known Issues

- **Unused redis dependency** — redis is listed in `pyproject.toml` but not imported by any
  endpoint. Left in place for potential future use by the result processor.

## Licensing

The code here is Apache-2.0 and needs no licence key to build, test or run. This service
performs **no licence enforcement**: it reads no licence environment variable and never calls a
licence server, so a clone with no licensing infrastructure behaves exactly like the deployed
instance.

The estate's optional licence gate is external and lives outside this repository: an Envoy
`ext_authz` filter consults [`license-authz`](https://github.com/nonagenticai/license-authz) only
for hosts listed in its `LICENSE_GATED_APP_KEY` table. The default is **no gate** — a host that
is not in that table is never licence-checked — and `python-test-executor` is not in it.

## License

Apache-2.0 — see [`LICENSE`](LICENSE). This repository has no `NOTICE` file.
