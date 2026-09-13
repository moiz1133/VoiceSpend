"""FX provider abstraction — same swappable pattern as the Phase 4
LLM/STT providers (app/services/extraction, app/services/stt): business
logic (app/services/fx/ingest.py) depends only on this ABC, never on a
concrete vendor, so switching providers is a config change plus one new
file, not a rewrite.
"""

from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal


class FXProvider(ABC):
    provider_name: str

    @property
    @abstractmethod
    def base_currency(self) -> str:
        """The currency the raw values from fetch_latest/fetch_for_date are
        quoted against — 'USD', 'EUR', or whatever this provider's plan
        fixes it to. Never assumed by callers; always read from here.
        """

    @abstractmethod
    async def fetch_latest(self) -> dict[str, Decimal]:
        """code -> raw value, quoted against `base_currency`, for whatever
        date the provider currently considers "latest".
        """

    @abstractmethod
    async def fetch_for_date(self, d: date) -> dict[str, Decimal]:
        """Same shape as fetch_latest, for a specific historical date."""
