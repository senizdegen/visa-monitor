# syntax=docker/dockerfile:1
# Base: python slim + Chromium installed via `playwright install --with-deps`.
# (mcr.microsoft.com/playwright/python:v1.65.0-noble was unreachable from the build network.)
FROM python:3.12-slim-bookworm AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# Dependencies first (cache layer), without the project itself.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# playwright's bundled node needs libatomic before it can even run the installer
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini adapters.yaml ./
RUN uv sync --frozen --no-dev --no-editable

RUN useradd --create-home --uid 1000 alay \
    && mkdir -p /data/evidence && chown -R alay:alay /data
ENV EVIDENCE_DIR=/data/evidence

# ---- runtime: no dev deps ----
FROM base AS runtime
USER alay
CMD ["uvicorn", "alay.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---- test: with dev deps and tests ----
FROM base AS test
RUN uv sync --frozen --no-editable
COPY tests ./tests
RUN chown -R alay:alay /app/tests
USER alay
CMD ["pytest", "-p", "no:cacheprovider"]
