# Voicespend backend

Voice-first, offline-first AI expense tracker — backend service.

> **Phase 7 status**: data model (Phase 2), sync API (Phase 3), the
> stateless parse pipeline (Phase 4), metering/entitlements (Phase 5), FX
> ingestion (Phase 6), and ops (Phase 7 — per-device rate limiting on the
> paid endpoints, Prometheus metrics + Grafana dashboard, structured JSON
> logging with request/trace correlation, and a hardened test suite) are
> all in place. This is the final backend phase — no new business
> endpoints in Phase 7, only `GET /metrics` (infra).
>
> **Rate limiting protects unit economics, never logging.** A per-device
> (falling back to per-user) atomic Redis limiter guards ONLY
> `POST /api/v1/parse` and `POST /api/v1/transcribe` — `/api/v1/sync` and
> the RevenueCat webhook are never rate limited. Exceeding it returns 429
> + `Retry-After`, which is a different signal from Phase 5's 200/
> `quota_exceeded`: 429 means "slow down," `quota_exceeded` means "upgrade."
> See `app/services/ratelimit/`.
>
> **Observability**: `GET /metrics` (Prometheus format, token-guarded via
> `METRICS_TOKEN`) and a starter Grafana dashboard at
> `dashboards/snapexpense.json` — see `app/core/metrics.py` for every
> metric this app emits and the "Background jobs" section below for
> bringing up Prometheus/Grafana locally.
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

## Observability (Phase 7)

**Metrics**: `GET /metrics` on the api serves Prometheus text format. Set
`METRICS_TOKEN` and scrape with `Authorization: Bearer <token>` — leaving
it blank leaves the endpoint open, which is only safe when this service
is reachable exclusively from a private network. See `app/core/metrics.py`
for the full metric list (HTTP durations, parse latency/cost/tokens by
stage, rate-limiter rejections, sync push results, FX ingest freshness,
webhook outcomes) — every label is a bounded set (route templates,
provider/model names, fixed status enums), never a user/device/trace id.

The Celery `worker` container also exposes its own metrics on port 9808
(fx_last_successful_ingest_timestamp, fx_ingest_failure_total) — a
separate scrape target from the api, since FX metrics are incremented in
the worker process, not the api process.

**Multiprocess mode**: set `PROMETHEUS_MULTIPROC_DIR` (already wired in
`docker-compose.yml` for `api` and `worker`, as container-local paths —
never share this directory between containers or you'll corrupt the
aggregated metrics, since prometheus_client's per-process filenames are
PID-based) whenever the api runs with more than one worker process.
Single-worker dev setups work fine without it.

**Local dashboards**: bring up Prometheus + Grafana alongside the app:

```bash
docker compose -f docker-compose.yml -f compose.observability.yml up --build
```

Grafana (`http://localhost:3000`, admin/admin) comes pre-provisioned with
a Prometheus datasource and the `dashboards/snapexpense.json` starter
dashboard (parse latency percentiles, cost/hour and cost-per-1k-logs unit
economics, parse status breakdown, 429 rate, new-logs/hour, FX ingest
freshness, webhook outcomes) — no manual import needed.

**Structured logging**: JSON lines by default (`LOG_JSON=true`), each
request's log lines correlated by `request_id` (from an `X-Request-Id`
header, generated if absent, echoed back) via `app/core/middleware.py`;
parse requests additionally carry the Langfuse `trace_id` — see
`app/core/logging.py`. Raw audio and secrets are never logged; the STT
transcript is logged only when `LOG_CAPTURE_TRANSCRIPT=true` (default
off), matching `LANGFUSE_CAPTURE_TRANSCRIPT`'s gate philosophy.

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

Most of this suite needs a real Postgres reachable at `DATABASE_URL` —
several invariants (the `xmax` trick behind sync idempotency/metering,
the `server_seq` trigger, `ON CONFLICT` upserts, partial unique indexes)
are genuinely dialect-specific and would silently pass-or-lie under
SQLite. Those tests are marked `@pytest.mark.pg`; a handful of pure-unit
tests (schema validation, normalization math, the metrics/logging
formatters) need neither DB nor Redis and aren't marked. Start Postgres
(and Redis, for the rate-limiter tests) first:

```bash
docker compose up -d postgres redis
uv run pytest                # everything
uv run pytest -m "not pg"    # fast unit-only subset (no DB needed)
uv run pytest -m pg          # integration subset only
uv run ruff check .
uv run mypy app
```

`tests/test_sync_idempotency_hardened.py` and `tests/test_parse_fuzz.py`
are the property/fuzz-style hardening tests for the two highest-risk
areas: the sync upsert's idempotency (replay counts, randomized
client_rev ordering, real concurrent pushes via independent connections)
and the extraction validation boundary (a battery of malformed LLM tool-
call outputs, asserting every one resolves to a clean `failed` — or
coerces where safe — and never a 500). `tests/test_response_contracts.py`
locks down the field set of every client-facing response shape so a
refactor can't silently break the mobile client's parsing.

## Environment variables

See `.env.example` for the full list with descriptions. At minimum, before
booting against a real Supabase project you must fill in:

- `SUPABASE_URL`
- `SUPABASE_JWT_SECRET` (or `SUPABASE_JWT_JWKS_URL` for asymmetric keys)
- `SUPABASE_SERVICE_KEY`

`DATABASE_URL` and `REDIS_URL` default to the docker-compose services and
don't need to change for local dev.
