"""Best-effort LLM cost estimation — the single source computed once per
call and fed to the parse_llm_cost_usd_total Prometheus counter (the
unit-economics panel: cost per 1k logs on a flat-fee tier). Deliberately
computed in exactly one place so this number and any other cost reference
in the app can never drift apart by construction.

Pricing is USD per 1K tokens and will go stale as vendors reprice models —
treat this as an approximation for dashboards, not a billing-grade
ledger. An unknown (provider, model) pair returns None rather than
guessing, and callers must treat None as "cost unknown," not as zero.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    input_per_1k_usd: float
    output_per_1k_usd: float


# Update as models/pricing move — deliberately small and explicit rather
# than trying to auto-derive from a vendor API that doesn't expose this.
_PRICING: dict[tuple[str, str], ModelPricing] = {
    ("anthropic", "claude-3-5-haiku-20241022"): ModelPricing(0.0008, 0.004),
    ("anthropic", "claude-3-5-sonnet-20241022"): ModelPricing(0.003, 0.015),
    ("gemini", "gemini-1.5-flash"): ModelPricing(0.000075, 0.0003),
    ("gemini", "gemini-2.0-flash"): ModelPricing(0.0001, 0.0004),
}


def estimate_cost_usd(
    *,
    provider: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> float | None:
    pricing = _PRICING.get((provider, model))
    if pricing is None or prompt_tokens is None or completion_tokens is None:
        return None
    return (
        (prompt_tokens / 1000) * pricing.input_per_1k_usd
        + (completion_tokens / 1000) * pricing.output_per_1k_usd
    )
