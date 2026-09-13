# Voicespend backend

Voice-first, offline-first AI expense tracker — backend service.

> **Phase 6 status**: data model (Phase 2), sync API (Phase 3), the
> stateless parse pipeline (Phase 4), metering/entitlements (Phase 5), and
> FX ingestion (Phase 6 — a daily Celery beat job that pulls FX rates into
> `fx_rates`, a manual backfill task, and a sweep that fills the
> `amount_base` NULLs the parse pipeline leaves behind) are all in place.
> No new HTTP endpoints in Phase 6 — see `app/worker/` and
> `app/services/fx/`.
>
> The parse endpoints never touch the `expenses` table — see the module
> docstring in `app/api/v1/parse.py`. LLM/STT/FX/tracing providers are all
> swappable via config (`LLM_PROVIDER`, `STT_PROVIDER`, `FX_PROVIDER`) —
> see `app/services/extraction/factory.py`, `app/services/stt/factory.py`,
> and `app/services/fx/factory.py`.
>
> **Quota never blocks logging.** `POST /api/v1/sync` always accepts and
> stores valid records, even far over quota — metering only counts and
> returns an `entitlement` signal block (see `app/schemas/entitlement.py`)
> that the client uses to show an upgrade nudge. The only thing quota gates
> is the paid LLM/STT call on `/parse` and `/transcribe`, to protect unit
> economics — see `app/services/metering/` and
> `app/services/entitlements/resolver.py`.
>
> **FX rates are USD-pivot and never synthesized for gaps.** Every
> provider payload is normalized to `rate_per_usd` before it's stored (see
> `app/services/fx/ingest.py`), with an exact `rate_per_usd('USD')=1`
> self-row always written. Weekend/holiday gaps are left as genuine gaps —
> `app/services/currency/converter.py` already falls back to the latest
> rate on or before the requested date, so nothing downstream needs to
> know a gap happened. `expenses.amount_base` NULLs get filled by
> `app/services/fx/recompute.py` via a plain SQL `UPDATE`, so the
> `expenses.server_seq` trigger fires and the enriched row is pulled by
> every device on its next `/api/v1/sync`.

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

## Background jobs (Celery)

`docker compose up` also brings up two Celery services sharing the API
image: `worker` (executes tasks) and `beat` (schedules them) — kept as
separate containers deliberately, since beat only schedules and should
never share a crash domain with the process that actually runs tasks.
The broker is Redis, on a separate DB index from the app's cache (see
`Settings.celery_broker_url`); no result backend is configured.

- `app/worker/tasks/fx.py:ingest_daily_fx` — beat-scheduled (`FX_INGEST_HOUR_UTC`,
  default 06:00 UTC). Pulls the latest rates, normalizes and upserts them,
  then chains `recompute_amount_base`.
- `app/worker/tasks/fx.py:recompute_amount_base` — fills `expenses.amount_base`
  NULLs in bounded batches (`FX_RECOMPUTE_BATCH`, default 500 rows/run).
- `app/worker/tasks/fx.py:backfill_fx_range` — manual seed/backfill for a
  date range, idempotent (safe to re-run over an overlapping range):

  ```bash
  docker compose exec worker uv run celery -A app.worker.celery_app:celery_app call \
    app.worker.tasks.fx.backfill_fx_range --args '["2026-08-01", "2026-09-13"]'
  ```

## Migrations

Migrations run with Alembic, configured for the async engine
(`alembic/env.py`). The initial migration creates all 7 tables
(`users`, `devices`, `expenses`, `categories`, `fx_rates`, `entitlements`,
`usage_counters`) with their indexes and constraints, and seeds the 8
default system categories. Later migrations add the `expenses.server_seq`
sync cursor (Phase 3) and `processed_webhook_events` +
`entitlements.last_event_ts_ms` (Phase 5, for the RevenueCat webhook's
idempotency and ordering guarantees).

```bash
uv run alembic upgrade head                                  # apply migrations
uv run alembic revision --autogenerate -m "add some_table"   # future changes
uv run alembic downgrade -1                                  # roll back one revision
```

After `alembic upgrade head`, `alembic revision --autogenerate` should
always produce an empty diff — if it doesn't, the models and the migration
have drifted and one of them needs fixing.

## Tests, lint, types

Model round-trip tests (`tests/test_models.py`, `tests/test_schemas.py`)
need a real Postgres reachable at `DATABASE_URL` — they build the schema
straight from the SQLAlchemy models (independent of Alembic) so they catch
model/DB mismatches directly. Health-check tests use fakes and don't need
it. Start Postgres first:

```bash
docker compose up -d postgres
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
