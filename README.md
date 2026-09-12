# Voicespend backend

Voice-first, offline-first AI expense tracker — backend service.

> **Phase 0 status**: this is project skeleton only — infra wiring and auth
> plumbing. There are no expense/sync/parse/billing endpoints yet; see the
> `TODO(Phase 2)` markers in `app/models/`, `app/schemas/`, and
> `app/api/v1/router.py`.

## Architecture note: Supabase for Auth/Storage, our own layer for data

We use Supabase to collapse Auth + Postgres + Storage into one dependency for
the MVP, but the codebase stays portable so we can migrate off it later:

- **Data access** always goes through our own async SQLAlchemy layer against
  `DATABASE_URL` — the Supabase client library is never used for reading or
  writing application data.
- **Auth** is handled by verifying Supabase-issued JWTs ourselves
  (`app/core/security.py`), exposed as a single `get_current_user` (and
  `get_optional_user`) FastAPI dependency. All Supabase-specific logic is
  isolated in that one module.
- **Storage** goes through the `StorageBackend` interface
  (`app/storage/base.py`), currently implemented against Supabase Storage
  (`app/storage/supabase_storage.py`). Swapping to R2/S3 later means adding
  one new implementation, not touching callers.

Locally, `DATABASE_URL` points at the Postgres container from
`docker-compose.yml`, not Supabase — Supabase is only used for hosted
environments and for Auth/Storage.

## Requirements

- [uv](https://docs.astral.sh/uv/) for dependency management
- Docker + Docker Compose for local Postgres/Redis (or the full stack)

## Setup

```bash
cp .env.example .env
# then fill in SUPABASE_URL, SUPABASE_JWT_SECRET (or SUPABASE_JWT_JWKS_URL),
# and SUPABASE_SERVICE_KEY from your Supabase project settings.
```

## Running locally

Bring up the full stack (API + Postgres + Redis) with hot reload:

```bash
docker compose up --build
```

The API is served at `http://localhost:8000`. Check it's healthy:

```bash
curl http://localhost:8000/api/v1/health/live
curl http://localhost:8000/api/v1/health/ready
```

Alternatively, run Postgres/Redis in Docker and the API on the host:

```bash
docker compose up -d postgres redis
uv sync
uv run uvicorn app.main:app --reload
```

## Migrations

Migrations run with Alembic, configured for the async engine
(`alembic/env.py`). The initial migration is a no-op that proves the
toolchain runs end to end — no tables exist yet (Phase 2 adds the first real
schema).

```bash
uv run alembic upgrade head          # apply migrations
uv run alembic revision --autogenerate -m "add expenses table"   # Phase 2+
uv run alembic downgrade -1          # roll back one revision
```

## Tests, lint, types

```bash
uv run pytest
uv run ruff check .
uv run mypy app
```

## Environment variables

See `.env.example` for the full list with descriptions. At minimum, before
booting against a real Supabase project you must fill in:

- `SUPABASE_URL`
- `SUPABASE_JWT_SECRET` (or `SUPABASE_JWT_JWKS_URL` for asymmetric keys)
- `SUPABASE_SERVICE_KEY`

`DATABASE_URL` and `REDIS_URL` default to the docker-compose services and
don't need to change for local dev.
