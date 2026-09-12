"""The only place in the codebase allowed to know which LLM provider is
configured. Everything else — the endpoint, tests — depends on the
LLMExtractor ABC and gets a concrete instance from `get_extractor()`.
"""

from app.core.config import get_settings
from app.services.extraction.base import LLMExtractor


def get_extractor() -> LLMExtractor:
    settings = get_settings()

    if settings.LLM_PROVIDER == "anthropic":
        from app.services.extraction.anthropic import AnthropicExtractor

        return AnthropicExtractor()

    if settings.LLM_PROVIDER == "gemini":
        from app.services.extraction.gemini import GeminiExtractor

        return GeminiExtractor()

    raise ValueError(f"Unknown LLM_PROVIDER: {settings.LLM_PROVIDER!r}")
