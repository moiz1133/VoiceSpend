"""Anthropic (Claude) implementation of LLMExtractor.

Only app/services/extraction/factory.py imports this module by name (via
settings.LLM_PROVIDER) — nothing else in the codebase should reference
`AnthropicExtractor` directly, or swapping providers stops being a config
change. Not exercised by tests directly: tests mock at the LLMExtractor
ABC boundary (see app/services/extraction/factory.py's dependency override
in tests/test_parse.py), so the exact Anthropic SDK call shape here isn't
what's under test — if the SDK's API surface has moved by the time this
runs for real, this is the one place to adjust it.
"""

from anthropic import AsyncAnthropic

from app.core.config import get_settings
from app.services.extraction.base import ExtractionContext, ExtractionResult, LLMExtractor
from app.services.extraction.prompt import build_system_prompt
from app.services.extraction.schema import (
    EXTRACTION_JSON_SCHEMA,
    EXTRACTION_TOOL_DESCRIPTION,
    EXTRACTION_TOOL_NAME,
    RawExtraction,
)


class AnthropicExtractor(LLMExtractor):
    provider_name = "anthropic"

    def __init__(self) -> None:
        settings = get_settings()
        self.model_name = settings.LLM_MODEL
        self._client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def extract(self, text: str, ctx: ExtractionContext) -> ExtractionResult:
        system_prompt = build_system_prompt(
            base_currency=ctx.base_currency,
            known_categories=ctx.known_categories,
            reference_time=ctx.reference_time,
        )

        try:
            response = await self._client.messages.create(
                model=self.model_name,
                max_tokens=1024,
                system=system_prompt,
                messages=[{"role": "user", "content": text}],
                tools=[
                    {
                        "name": EXTRACTION_TOOL_NAME,
                        "description": EXTRACTION_TOOL_DESCRIPTION,
                        "input_schema": EXTRACTION_JSON_SCHEMA,
                    }
                ],
                tool_choice={"type": "tool", "name": EXTRACTION_TOOL_NAME},
            )
        except Exception as exc:  # noqa: BLE001 - any provider failure -> ExtractionResult.error
            return ExtractionResult(raw=None, error=f"anthropic request failed: {exc}")

        tool_use = next(
            (block for block in response.content if block.type == "tool_use"), None
        )
        if tool_use is None:
            return ExtractionResult(raw=None, error="anthropic response had no tool_use block")

        try:
            raw = RawExtraction.model_validate(tool_use.input)
        except Exception as exc:  # noqa: BLE001 - malformed output -> failed, not a crash
            return ExtractionResult(raw=None, error=f"malformed extraction output: {exc}")

        usage = response.usage
        return ExtractionResult(
            raw=raw,
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
        )
