"""Schemas for POST /api/v1/parse and POST /api/v1/transcribe.

Both endpoints are stateless extraction — see app/api/v1/parse.py's module
docstring for the hard boundary these schemas exist within (never touch
the expenses table; amount_base here is a best-effort display value only).
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

ParseStatusLiteral = Literal["ok", "low_confidence", "failed"]


class ParseRequest(BaseModel):
    text: str
    # The client's local "now", for resolving relative dates ("yesterday").
    # Defaults to server time when omitted — see app/api/v1/parse.py.
    reference_time: datetime | None = None


class ExtractionOut(BaseModel):
    amount_original: Decimal | None
    currency_original: str | None
    amount_base: Decimal | None
    currency_base: str | None
    category: str | None
    category_is_known: bool
    payment_method: str | None
    merchant: str | None
    spent_at: datetime | None
    confidence: float = Field(ge=0, le=1)


class ParseResponse(BaseModel):
    status: ParseStatusLiteral
    extraction: ExtractionOut | None
    trace_id: str | None = None


class TranscribeResponse(ParseResponse):
    transcript: str
