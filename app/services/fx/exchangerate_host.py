"""exchangerate.host provider.

Its free tier fixes the quote currency to EUR (base=USD requires a paid
access_key) — a useful default specifically because it's NOT USD, so the
USD-pivot cross-conversion in app/services/fx/ingest.py gets exercised in
production, not just in tests. If a paid key with USD access is
configured, `base_currency` still correctly reports "USD" so the ingest
step takes the direct-passthrough branch instead.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

from app.services.fx.base import FXProvider

_BASE_URL = "https://api.exchangerate.host"


class ExchangerateHostProvider(FXProvider):
    provider_name = "exchangerate_host"

    def __init__(self, api_key: str | None = None, *, base_currency: str = "EUR") -> None:
        self._api_key = api_key or None
        self._base_currency = base_currency.upper()

    @property
    def base_currency(self) -> str:
        return self._base_currency

    async def fetch_latest(self) -> dict[str, Decimal]:
        return await self._fetch("/latest")

    async def fetch_for_date(self, d: date) -> dict[str, Decimal]:
        return await self._fetch(f"/{d.isoformat()}")

    async def _fetch(self, path: str) -> dict[str, Decimal]:
        params: dict[str, str] = {}
        if self._api_key:
            params["access_key"] = self._api_key

        async with httpx.AsyncClient(base_url=_BASE_URL, timeout=10.0) as client:
            response = await client.get(path, params=params)
            response.raise_for_status()
            payload = response.json()

        raw_rates = payload.get("rates", {})
        values: dict[str, Decimal] = {}
        for code, value in raw_rates.items():
            try:
                values[code.upper()] = Decimal(str(value))
            except (InvalidOperation, TypeError):
                continue
        return values
