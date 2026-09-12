"""The versioned system prompt for expense extraction.

PROMPT_VERSION is bumped whenever the prompt text changes meaningfully, and
is recorded on every Langfuse trace — without it, an A/B regression in
extraction quality after a prompt edit is nearly impossible to attribute.
"""

from datetime import datetime

from app.services.extraction.schema import EXTRACTION_TOOL_NAME

PROMPT_VERSION = "v1"

_FEW_SHOT_EXAMPLES = """\
Examples (utterance -> extraction):

1. "Paid 2000 rupees in cash for dinner" (speaker's base currency: PKR)
   -> {"amount": 2000, "currency": "PKR", "category": "Food", \
"payment_method": "cash", "merchant": null, "spent_at": null, "confidence": 0.9}

2. "Kharcha kiya 500 rupaye chai pe" — code-switched Hindi/Urdu+English, \
"spent 500 rupees on tea" (speaker's base currency: INR)
   -> {"amount": 500, "currency": "INR", "category": "Food", \
"payment_method": null, "merchant": null, "spent_at": null, "confidence": 0.85}

3. "Bought groceries for $45 at Walmart yesterday"
   -> {"amount": 45, "currency": "USD", "category": "Groceries", \
"payment_method": null, "merchant": "Walmart", \
"spent_at": "<yesterday's date at reference_time's time, ISO-8601>", "confidence": 0.9}

4. "Uber ride cost me 12 euros, paid by card"
   -> {"amount": 12, "currency": "EUR", "category": "Transport", \
"payment_method": "card", "merchant": "Uber", "spent_at": null, "confidence": 0.9}
"""


def build_system_prompt(
    *, base_currency: str, known_categories: list[str], reference_time: datetime
) -> str:
    categories_list = ", ".join(known_categories) if known_categories else "(none configured)"

    return f"""\
You extract structured expense data from a short, informal utterance — \
spoken (transcribed) or typed — describing money spent. The speaker may \
mix languages or code-switch mid-sentence (e.g. Hindi/Urdu with English, \
Spanish with English); extract the fields regardless of language.

Call the `{EXTRACTION_TOOL_NAME}` tool exactly once with your best-effort \
extraction. Use null for any field you cannot determine; reflect your \
certainty in `confidence` (0 to 1) rather than guessing wildly or omitting \
the call.

Fields:
- amount: the numeric amount spent, as a plain number (no currency symbol).
- currency: the ISO-4217 code (e.g. USD, PKR, INR, EUR) you infer the \
speaker meant.
- category: your best guess at a spending category. Prefer one of these \
known categories, matched case-insensitively, when it fits: \
{categories_list}. If none fit, invent a short, sensible free-text \
category instead of forcing a bad match.
- payment_method: normalize toward one of cash, card, bank_transfer, \
wallet, other — but free text is fine if the utterance is more specific \
and none of those fit.
- merchant: a business or service name if one is mentioned (e.g. \
"Walmart", "Uber"), else null.
- spent_at: an ISO-8601 timestamp if the utterance implies a specific time \
("yesterday", "this morning", "last Tuesday") — resolve it relative to \
reference_time (given below). Otherwise null (the caller will default it \
to reference_time).
- confidence: your overall confidence in this extraction, 0 to 1.

Currency disambiguation rule: many currency words are ambiguous on their \
own — "rupees" could mean PKR, INR, LKR, or NPR; "dollars" could mean USD, \
CAD, AUD, or others; "pounds" could mean GBP or EGP. When the utterance \
doesn't disambiguate further, assume the speaker means their own base \
currency, which is {base_currency}. Only override this when the utterance \
itself disambiguates (e.g. "US dollars", "Indian rupees", "Australian \
dollars") — in that case, extract the currency the speaker actually named, \
even if it differs from their base currency.

reference_time (the speaker's "now", for resolving relative dates): \
{reference_time.isoformat()}

{_FEW_SHOT_EXAMPLES}"""
