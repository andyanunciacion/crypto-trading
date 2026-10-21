# Build context is the repo root (see compose.yaml), because the uv workspace and
# its lockfile live there, and later slices also copy shared/.
FROM python:3.12.15-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /bin/uv

# Precompile .pyc for faster startup; copy files out of the uv cache (a separate
# mount) instead of hard-linking; use the image's Python, never download one.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 1) Third-party dependencies only. Docker caches this layer until uv.lock or a
#    pyproject.toml changes, so code-only edits rebuild in seconds.
COPY pyproject.toml uv.lock .python-version ./
COPY backend/pyproject.toml backend/pyproject.toml
COPY engine/pyproject.toml engine/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package backend --no-install-workspace

# 2) Our own code.
COPY backend/src backend/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package backend

ENV PATH="/app/.venv/bin:$PATH"

# Run as an unprivileged user, not root.
RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
