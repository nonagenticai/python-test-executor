# Python Test Executor
# Source: Dockerfile.testing target python-testing (consolidated)
#
# ONE stage from the canonical upstream image, pinned by TAG: the tag keeps the
# revision resolvable on the public registry, and this service carries no
# toolchain that would justify a separately published base image.
FROM docker.io/library/python:3.14-slim

RUN apt-get update && apt-get install -y \
    gcc g++ curl git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

# Install EXACTLY what uv.lock pins, into /app/.venv. uv.lock is REQUIRED (no
# glob): a missing lock fails the build and the image never re-resolves. The old
# `uv pip install --system <hand pins>` line never read the lock, so prod ran
# fastapi 0.104.1 / starlette 0.27.0 / anyio 3.7.1 (17 known vulns) while CI
# tested and audited the lock (gitops#4248, #4253 image-vs-lock gate).
# pytest + hypothesis are RUNTIME deps in pyproject: /execute shells out to
# `sys.executable -m pytest`, and sys.executable is the venv python (PATH below).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

# Build-time guard: fail the build if any installed version != uv.lock.
# Same script as media-discovery / license-authz#63.
COPY scripts/assert_lock_versions.py /tmp/assert_lock_versions.py
RUN uv export --frozen --no-dev --no-emit-project --no-hashes -o /tmp/lock-pins.txt && \
    /app/.venv/bin/python /tmp/assert_lock_versions.py uv.lock /tmp/lock-pins.txt && \
    uv sync --frozen --no-dev --no-install-project --check && \
    /app/.venv/bin/python -m pytest --version && \
    rm -f /tmp/assert_lock_versions.py /tmp/lock-pins.txt

# Static-analysis CLIs available to code executed in this container: astral ruff
# (lint + isort `I` + format; replaces black/isort/pylint/flake8/autoflake) and
# ty (type checking; replaces mypy/pyright), plus bandit. src/result_processor.py
# parses exactly these output shapes (`ruff check --output-format json`,
# `ty check --output-format gitlab`), so ruff and ty are pinned exactly.
# `uv tool install` puts each in its OWN venv under /opt/uv-tools with only the
# entry point on PATH: they are deliberately NOT in uv.lock / /app/.venv, so the
# lock gate above (and the CI image-vs-lock gate, which reads the venv python)
# still sees exactly the locked app dependencies, and this repo's own dev pins
# (ruff/ty in the dev group) cannot conflict with these.
ENV UV_TOOL_DIR=/opt/uv-tools \
    UV_TOOL_BIN_DIR=/usr/local/bin
RUN uv tool install --no-cache "ruff==0.17.3" && \
    uv tool install --no-cache "ty==0.0.92" && \
    uv tool install --no-cache "bandit==1.9.4" && \
    ruff --version && ty --version && bandit --version

# Not `chmod -R /app`: that would walk /app/.venv and re-copy every file into a
# second, non-dedupable layer.
RUN mkdir -p /app/test /app/config /app/logs /tmp/pytest && \
    chmod 755 /app /app/test /app/config /app/logs && chmod -R 755 /tmp/pytest

COPY src/ ./src/

ENV PYTHONPATH=/app:/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TEST_MODE=true

EXPOSE 8087

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8087/health || exit 1

CMD ["python", "-m", "uvicorn", "test_service:app", "--host", "0.0.0.0", "--port", "8087"]
