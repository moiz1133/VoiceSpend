"""The provider-agnostic extraction interface.

Every LLM provider (Anthropic, Gemini, whatever comes next) implements
`LLMExtractor.extract()` and nothing else is allowed to know which
provider is in play — see factory.py for the only place that reads
settings.LLM_PROVIDER.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from app.services.extraction.schema import RawExtraction


@dataclass(frozen=True)
class ExtractionContext:
    base_currency: str
    known_categories: list[str]
    reference_time: datetime


@dataclass
class ExtractionResult:
    """The extractor's outcome, including enough metadata for tracing.

    `raw` is None when the provider call failed (network error, timeout —
    though timeouts are usually enforced by the caller, not here) or the
    provider's response didn't validate against RawExtraction; `error`
    carries a short human-readable reason in that case. This is a returned
    value rather than a raised exception specifically so "malformed output"
    and "provider error" are both ordinary, non-crashing outcomes for the
    endpoint to map to status="failed".
    """

    raw: RawExtraction | None
    error: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None


class LLMExtractor(ABC):
    provider_name: str
    model_name: str

    @abstractmethod
    async def extract(self, text: str, ctx: ExtractionContext) -> ExtractionResult:
        """Run structured extraction over `text`. Must not raise for
        ordinary provider/validation failures — see ExtractionResult.error.
        Genuine bugs (misconfiguration, programming errors) may still
        raise; the caller enforces the request-level timeout.
        """
        raise NotImplementedError
