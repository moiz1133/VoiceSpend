"""Provider-agnostic FX normalization + upsert.

The USD-pivot rule lives here exactly once, regardless of which provider's
raw payload feeds it — see normalize_to_usd_pivot's docstring for the
rule itself.
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FxRate
from app.services.fx.base import FXProvider

logger = logging.getLogger(__name__)

_SCALE = Decimal("0.00000001")
_USD = "USD"


@dataclass(frozen=True)
class IngestResult:
    rate_date: date
    currencies_written: int
    skipped: tuple[str, ...]


def normalize_to_usd_pivot(base_currency: str, values: dict[str, Decimal]) -> dict[str, Decimal]:
    """Converts a provider's raw payload (quoted against `base_currency`)
    into rate_per_usd for every currency in it, plus an always-present,
    exact USD self-row.

    - provider base == USD: values ARE already rate_per_usd; pass through.
    - provider base == X (e.g. EUR): values are "units of currency per 1 X".
      rate_per_usd(C) = value(C) / value(USD) — dividing "C per X" by
      "USD per X" cancels X, leaving "C per USD". Requires the payload to
      include a USD leg; if it's missing (or non-positive/non-finite), we
      can't pivot anything and only the forced USD=1 row comes out.
    - Any individual currency with a non-finite or non-positive computed
      rate is skipped (logged), not written — one bad entry never blocks
      the rest of the payload.
    - The USD row is always forced to exactly Decimal("1.00000000")
      afterward, never left to float-style computation — every conversion
      in app/services/currency/converter.py pivots through it, so it must
      exist and be exact.
    """
    base_currency = base_currency.upper()

    if base_currency == _USD:
        candidates = dict(values)
    else:
        usd_value = values.get(_USD)
        if usd_value is None or not usd_value.is_finite() or usd_value <= 0:
            logger.warning(
                "fx ingest: provider base %s payload has no usable USD leg to pivot "
                "on; only the USD self-row will be written this run",
                base_currency,
            )
            candidates = {}
        else:
            candidates = {code: value / usd_value for code, value in values.items()}

    normalized: dict[str, Decimal] = {}
    for code, rate in candidates.items():
        if not rate.is_finite() or rate <= 0:
            logger.warning("fx ingest: skipping %s, non-finite or non-positive rate %s", code, rate)
            continue
        normalized[code.upper()] = rate.quantize(_SCALE)

    normalized[_USD] = Decimal("1.00000000")
    return normalized


async def upsert_rates(
    db: AsyncSession, rates: dict[str, Decimal], rate_date: date, *, source: str
) -> int:
    """INSERT ... ON CONFLICT (currency_code, rate_date) DO UPDATE — safe to
    run twice for the same date (a re-run just refreshes that day's row).
    History is never deleted; there's no DELETE anywhere in this module.
    """
    if not rates:
        return 0

    stmt = pg_insert(FxRate).values(
        [
            {"currency_code": code, "rate_date": rate_date, "rate_per_usd": rate, "source": source}
            for code, rate in rates.items()
        ]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[FxRate.currency_code, FxRate.rate_date],
        set_={
            "rate_per_usd": stmt.excluded.rate_per_usd,
            "source": stmt.excluded.source,
            "updated_at": func.now(),
        },
    )
    await db.execute(stmt)
    await db.commit()
    return len(rates)


async def ingest_snapshot(
    db: AsyncSession, *, provider: FXProvider, values: dict[str, Decimal], rate_date: date
) -> IngestResult:
    """One provider payload -> normalized -> upserted, for a single date."""
    normalized = normalize_to_usd_pivot(provider.base_currency, values)
    original_codes = {code.upper() for code in values}
    skipped = tuple(sorted(original_codes - set(normalized)))

    written = await upsert_rates(db, normalized, rate_date, source=provider.provider_name)
    return IngestResult(rate_date=rate_date, currencies_written=written, skipped=skipped)


async def backfill_range(
    db: AsyncSession, *, provider: FXProvider, start: date, end: date
) -> list[IngestResult]:
    """Seeds/refreshes one row per supported date in [start, end] (inclusive).

    FX markets are closed weekends/holidays, so a provider may simply have
    no data for some dates — that's not an error, we just skip that date
    and move on. app/services/currency/converter.py already looks up "the
    rate on or before spent_at's date", so a gap here is handled downstream
    without needing a synthesized row.
    """
    results: list[IngestResult] = []
    current = start
    while current <= end:
        try:
            values = await provider.fetch_for_date(current)
        except Exception:  # noqa: BLE001 - one bad date must not abort the whole range
            logger.exception("fx backfill: fetch_for_date failed for %s", current)
            current += timedelta(days=1)
            continue
        results.append(
            await ingest_snapshot(db, provider=provider, values=values, rate_date=current)
        )
        current += timedelta(days=1)
    return results
