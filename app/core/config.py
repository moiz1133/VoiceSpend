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

    @property
    def cors_origins(self) -> list[str]:
        if self.CORS_ALLOW_ORIGINS.strip() == "*":
            return ["*"]
        return [origin.strip() for origin in self.CORS_ALLOW_ORIGINS.split(",") if origin.strip()]

    @property
    def supabase_issuer(self) -> str:
        return self.SUPABASE_JWT_ISSUER or f"{self.SUPABASE_URL.rstrip('/')}/auth/v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
