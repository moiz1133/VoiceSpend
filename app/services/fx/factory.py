"""Returns the configured FXProvider — the only place a provider name is
read from settings. See app/services/extraction/factory.py for the same
pattern applied to LLM providers.
"""

from app.core.config import get_settings
from app.services.fx.base import FXProvider


def get_fx_provider() -> FXProvider:
    settings = get_settings()

    if settings.FX_PROVIDER == "exchangerate_host":
        from app.services.fx.exchangerate_host import ExchangerateHostProvider

        return ExchangerateHostProvider(api_key=settings.FX_PROVIDER_API_KEY or None)

    raise ValueError(f"Unknown FX_PROVIDER: {settings.FX_PROVIDER}")
