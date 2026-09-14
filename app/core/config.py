"""Application settings, read from environment variables / .env."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ENV: Literal["local", "staging", "production"] = "local"
    LOG_LEVEL: str = "INFO"

    # Database (SQLAlchemy / asyncpg). Local dev -> compose Postgres,
    # staging/prod -> Supabase Postgres connection string.
    DATABASE_URL: str

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Supabase — Auth + Storage only, never used for data access.
    SUPABASE_URL: str
    SUPABASE_JWT_SECRET: str = ""
    SUPABASE_JWT_JWKS_URL: str = ""
    SUPABASE_JWT_AUDIENCE: str = "authenticated"
    SUPABASE_JWT_ISSUER: str = ""
    SUPABASE_SERVICE_KEY: str = ""
    SUPABASE_STORAGE_BUCKET: str = "voicespend"

    # CORS
    CORS_ALLOW_ORIGINS: str = "*"

    # Parse pipeline — provider choice is config, never hardcoded in logic.
    LLM_PROVIDER: Literal["anthropic", "gemini"] = "anthropic"
    LLM_MODEL: str = ""
    STT_PROVIDER: Literal["groq", "deepgram"] = "groq"
    STT_MODEL: str = ""
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    DEEPGRAM_API_KEY: str = ""

    PARSE_TIMEOUT_S: float = 3.0
    PARSE_MAX_TEXT_LENGTH: int = 500
    MAX_AUDIO_BYTES: int = 5 * 1024 * 1024

    # Below LOW -> "failed"; [LOW, OK) -> "low_confidence"; >= OK -> "ok".
    CONFIDENCE_OK_THRESHOLD: float = 0.75
    CONFIDENCE_LOW_THRESHOLD: float = 0.4

    # Langfuse — no-op throughout the app when keys are unset.
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"
    LANGFUSE_CAPTURE_TRANSCRIPT: bool = True

    # Metering & entitlements (Phase 5). Quota gates paid LLM/STT calls on
    # /parse and /transcribe only — it NEVER blocks /api/v1/sync.
    FREE_MONTHLY_LOG_QUOTA: int = 20

    # Shared secret RevenueCat sends as a plain Authorization header value
    # (configured in the RevenueCat dashboard) — not an HMAC signature.
    # Blank disables the webhook entirely (every request gets 401).
    REVENUECAT_WEBHOOK_AUTH: str = ""

    # FX ingestion (Phase 6). Provider/key are config, never hardcoded —
    # see app/services/fx/factory.py.
    FX_PROVIDER: Literal["exchangerate_host"] = "exchangerate_host"
    FX_PROVIDER_API_KEY: str = ""
    # UTC hour the daily ingest beat task fires (0-23).
    FX_INGEST_HOUR_UTC: int = 6
    # Max expenses.amount_base NULLs recomputed per task run — keeps each
    # sweep a bounded transaction rather than one giant backlog drain.
    FX_RECOMPUTE_BATCH: int = 500

    # Celery (Phase 6) — broker only, no result backend needed for these
    # tasks. Leave blank to derive from REDIS_URL (see celery_broker_url)
    # on a separate DB index so broker keys never collide with the cache's.
    CELERY_BROKER_URL: str = ""

    # Rate limiting (Phase 7) — /api/v1/parse and /api/v1/transcribe ONLY.
    # Never applied to /api/v1/sync or the RevenueCat webhook. Two windows
    # are always both checked: a per-minute burst limit and a per-day
    # ceiling. See app/services/ratelimit/.
    PARSE_RATE_LIMIT: int = 20
    PARSE_RATE_LIMIT_DAILY: int = 300
    # If Redis is unreachable: True = allow the request through (the
    # Phase 5 monthly quota remains the real economic backstop for free
    # users); False = reject with 503. A conscious config choice, not a
    # silent default — see app/services/ratelimit/dependency.py.
    RATE_LIMIT_FAIL_OPEN: bool = True

    # Observability (Phase 7).
    # Static bearer token required on GET /metrics. Leave blank to leave it
    # open (fine for local dev / a network already scoped to internal
    # scrapers only) — see app/api/metrics.py.
    METRICS_TOKEN: str = ""
    # Structured logs as one JSON object per line (production default).
    # False -> a plain human-readable formatter for local dev consoles.
    LOG_JSON: bool = True
    # Include the STT transcript text in our own structured logs. Same
    # gate philosophy as LANGFUSE_CAPTURE_TRANSCRIPT — default off.
    LOG_CAPTURE_TRANSCRIPT: bool = False

    @property
    def cors_origins(self) -> list[str]:
        if self.CORS_ALLOW_ORIGINS.strip() == "*":
            return ["*"]
        return [origin.strip() for origin in self.CORS_ALLOW_ORIGINS.split(",") if origin.strip()]

    @property
    def supabase_issuer(self) -> str:
        return self.SUPABASE_JWT_ISSUER or f"{self.SUPABASE_URL.rstrip('/')}/auth/v1"

    @property
    def celery_broker_url(self) -> str:
        if self.CELERY_BROKER_URL:
            return self.CELERY_BROKER_URL
        base = self.REDIS_URL.rsplit("/", 1)[0]
        return f"{base}/1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
