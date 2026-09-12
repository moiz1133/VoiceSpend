# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --create-home app

# ---------------------------------------------------------------------------
# dev: full dependency set (incl. dev/test tools), used by docker-compose
# with a bind-mounted source tree and --reload.
# ---------------------------------------------------------------------------
FROM base AS dev
COPY pyproject.toml ./
RUN uv sync --no-install-project
COPY . .
RUN uv sync && chown -R app:app /app
USER app
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---------------------------------------------------------------------------
# builder: production dependency set only, no dev tools.
# ---------------------------------------------------------------------------
FROM base AS builder
COPY pyproject.toml ./
RUN uv sync --no-install-project --no-dev
COPY . .
RUN uv sync --no-dev

# ---------------------------------------------------------------------------
# production: slim final image, non-root, no reload.
# ---------------------------------------------------------------------------
FROM base AS production
COPY --from=builder --chown=app:app /app /app
USER app
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
