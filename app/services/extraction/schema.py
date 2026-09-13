"""The extraction JSON schema — single source of truth.

`RawExtraction` is both the runtime validator for whatever a provider
returns (its structured-output / tool-calling mode is *pinned* to this
schema) and the schema definition itself, via `EXTRACTION_JSON_SCHEMA`. One
provider's tool/function-calling wrapper differs from another's, but both
`AnthropicExtractor` and `GeminiExtractor` build their provider-specific
tool definitions from this exact schema, so drift between "what we ask for"
and "what we validate" is structurally impossible.
"""

from typing import Any

from pydantic import BaseModel, Field

EXTRACTION_TOOL_NAME = "record_expense_extraction"
EXTRACTION_TOOL_DESCRIPTION = (
    "Record the structured fields extracted from a spoken or typed expense "
    "description. Always call this tool exactly once with your best-effort "
    "extraction, even if some fields are uncertain — use null for fields "
    "you genuinely cannot determine, and reflect your certainty in "
    "`confidence` rather than omitting the call."
)


class RawExtraction(BaseModel):
    """Exactly what the LLM returns — pre-currency-conversion, pre-category
    canonicalization. See app/api/v1/parse.py for the post-processing that
    turns this into the response the client sees.
    """

    # ge=0: a negative expense amount isn't a valid extraction — reject it
    # at the schema layer (raises, caught by the provider as a malformed-
    # output failure) rather than silently persisting a nonsensical value.
    amount: float | None = Field(default=None, ge=0)
    currency: str | None = None
    category: str | None = None
    payment_method: str | None = None
    merchant: str | None = None
    spent_at: str | None = None
    confidence: float = Field(ge=0, le=1)


def _tool_input_schema() -> dict[str, Any]:
    schema = RawExtraction.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return schema


EXTRACTION_JSON_SCHEMA: dict[str, Any] = _tool_input_schema()
