"""Gemini implementation of LLMExtractor.

See the note at the top of anthropic.py — this module is only ever loaded
via app/services/extraction/factory.py, and tests mock at the LLMExtractor
ABC boundary rather than the Gemini SDK, so this is the one place to touch
if the SDK's call shape has moved by the time this runs for real.
"""

from google import genai
from google.genai import types

from app.core.config import get_settings
from app.services.extraction.base import ExtractionContext, ExtractionResult, LLMExtractor
from app.services.extraction.prompt import build_system_prompt
from app.services.extraction.schema import RawExtraction


class GeminiExtractor(LLMExtractor):
    provider_name = "gemini"

    def __init__(self) -> None:
        settings = get_settings()
        self.model_name = settings.LLM_MODEL
        self._client = genai.Client(api_key=settings.GEMINI_API_KEY)

    async def extract(self, text: str, ctx: ExtractionContext) -> ExtractionResult:
        system_prompt = build_system_prompt(
            base_currency=ctx.base_currency,
            known_categories=ctx.known_categories,
            reference_time=ctx.reference_time,
        )

        try:
            response = await self._client.aio.models.generate_content(
                model=self.model_name,
                contents=text,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_schema=RawExtraction,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - any provider failure -> ExtractionResult.error
            return ExtractionResult(raw=None, error=f"gemini request failed: {exc}")

        parsed = response.parsed
        if isinstance(parsed, RawExtraction):
            raw = parsed
        else:
            if not response.text:
                return ExtractionResult(raw=None, error="gemini returned an empty response")
            try:
                raw = RawExtraction.model_validate_json(response.text)
            except Exception as exc:  # noqa: BLE001 - malformed output -> failed, not a crash
                return ExtractionResult(raw=None, error=f"malformed extraction output: {exc}")

        usage = response.usage_metadata
        return ExtractionResult(
            raw=raw,
            prompt_tokens=usage.prompt_token_count if usage else None,
            completion_tokens=usage.candidates_token_count if usage else None,
        )
