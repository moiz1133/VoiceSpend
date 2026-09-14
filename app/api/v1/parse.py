"""POST /api/v1/parse and POST /api/v1/transcribe — stateless extraction.

HARD BOUNDARY: neither endpoint reads or writes the `expenses` table, or
any table besides the authenticated user's row (for base_currency) and
the system categories (for the known-category prompt hint). They take
input, return structured JSON, and return — nothing here is persisted.
The client merges the result into its own optimistic record, bumps
client_rev, and pushes via /api/v1/sync. If you feel the urge to enrich an
expense row here, stop: that's the client's job via sync, and authoritative
FX (amount_base as written to the DB) is Phase 6's job, not this one. The
`amount_base` computed here is a best-effort display value only.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_db_user
from app.core.config import get_settings
from app.core.logging import trace_id_var
from app.core.metrics import (
    record_parse_cost,
    record_parse_latency,
    record_parse_request,
    record_parse_tokens,
)
from app.core.tracing import current_trace_id, traced_operation
from app.db.session import get_db
from app.models import Category, User
from app.schemas._validators import normalize_currency_code_or_none
from app.schemas.entitlement import EntitlementSignal
from app.schemas.parse import ExtractionOut, ParseRequest, ParseResponse, TranscribeResponse
from app.services.currency.converter import convert
from app.services.extraction.base import ExtractionContext, ExtractionResult, LLMExtractor
from app.services.extraction.factory import get_extractor
from app.services.extraction.pricing import estimate_cost_usd
from app.services.extraction.prompt import PROMPT_VERSION
from app.services.extraction.schema import RawExtraction
from app.services.metering.signal import build_entitlement_signal
from app.services.ratelimit.dependency import enforce_parse_rate_limit
from app.services.stt.base import STTProvider
from app.services.stt.factory import get_stt_provider

logger = logging.getLogger(__name__)

router = APIRouter(tags=["parse"])


async def _known_category_names(db: AsyncSession) -> list[str]:
    result = await db.execute(select(Category.name).where(Category.is_system.is_(True)))
    return list(result.scalars().all())


def _match_category(raw_category: str | None, known: list[str]) -> tuple[str | None, bool]:
    if raw_category is None:
        return None, False
    for name in known:
        if name.casefold() == raw_category.casefold():
            return name, True
    return raw_category, False


def _parse_spent_at(raw_spent_at: str | None, reference_time: datetime) -> datetime:
    if raw_spent_at is None:
        return reference_time
    try:
        parsed = datetime.fromisoformat(raw_spent_at)
    except ValueError:
        return reference_time
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=reference_time.tzinfo or UTC)
    return parsed


async def _build_extraction_out(
    raw: RawExtraction,
    *,
    db: AsyncSession,
    user: User,
    known_categories: list[str],
    reference_time: datetime,
) -> ExtractionOut:
    amount_original = Decimal(str(raw.amount)) if raw.amount is not None else None
    # Untrusted LLM output: normalize to the 3-char uppercase ISO-4217
    # invariant, or drop it to None — never store/return a malformed code.
    currency_original = normalize_currency_code_or_none(raw.currency)
    spent_at = _parse_spent_at(raw.spent_at, reference_time)
    category, category_is_known = _match_category(raw.category, known_categories)

    amount_base: Decimal | None = None
    if amount_original is not None and currency_original is not None:
        amount_base = await convert(
            db, amount_original, currency_original, user.base_currency, spent_at.date()
        )

    return ExtractionOut(
        amount_original=amount_original,
        currency_original=currency_original,
        amount_base=amount_base,
        currency_base=user.base_currency,
        category=category,
        category_is_known=category_is_known,
        payment_method=raw.payment_method,
        merchant=raw.merchant,
        spent_at=spent_at,
        confidence=raw.confidence,
    )


async def _run_extraction(
    *,
    text: str,
    user: User,
    db: AsyncSession,
    reference_time: datetime,
    extractor: LLMExtractor,
    entitlement: EntitlementSignal,
) -> ParseResponse:
    settings = get_settings()
    known_categories = await _known_category_names(db)
    ctx = ExtractionContext(
        base_currency=user.base_currency,
        known_categories=known_categories,
        reference_time=reference_time,
    )
    total_start = time.perf_counter()

    with traced_operation("parse_expense", input={"text": text}) as root:
        trace_id = current_trace_id()
        # Bind trace_id into every log line emitted for the rest of this
        # parse — see app/core/logging.py — so a log line can be traced
        # straight back to its Langfuse generation.
        trace_token = trace_id_var.set(trace_id) if trace_id else None

        try:
            with traced_operation(
                "llm_extract",
                as_type="generation",
                input={"text": text, "base_currency": user.base_currency},
                model=extractor.model_name,
            ) as generation:
                llm_start = time.perf_counter()
                try:
                    outcome = await asyncio.wait_for(
                        extractor.extract(text, ctx), timeout=settings.PARSE_TIMEOUT_S
                    )
                except Exception as exc:  # noqa: BLE001 - timeout/provider error -> failed
                    outcome = ExtractionResult(raw=None, error=str(exc))
                record_parse_latency(
                    provider=extractor.provider_name,
                    stage="llm",
                    seconds=time.perf_counter() - llm_start,
                )

                cost_usd = estimate_cost_usd(
                    provider=extractor.provider_name,
                    model=extractor.model_name,
                    prompt_tokens=outcome.prompt_tokens,
                    completion_tokens=outcome.completion_tokens,
                )
                record_parse_tokens(
                    provider=extractor.provider_name,
                    model=extractor.model_name,
                    kind="prompt",
                    count=outcome.prompt_tokens,
                )
                record_parse_tokens(
                    provider=extractor.provider_name,
                    model=extractor.model_name,
                    kind="completion",
                    count=outcome.completion_tokens,
                )
                if cost_usd is not None:
                    record_parse_cost(
                        provider=extractor.provider_name,
                        model=extractor.model_name,
                        cost_usd=cost_usd,
                    )

                generation.update(
                    output=outcome.raw.model_dump() if outcome.raw else None,
                    usage_details={
                        "input": outcome.prompt_tokens,
                        "output": outcome.completion_tokens,
                    },
                    metadata={
                        "provider": extractor.provider_name,
                        "prompt_version": PROMPT_VERSION,
                        "error": outcome.error,
                        "cost_usd": cost_usd,
                    },
                )

            status, extraction = await _to_response(
                outcome, db=db, user=user, known_categories=known_categories,
                reference_time=reference_time,
            )
            root.update(output={"status": status}, metadata={"status": status})
        finally:
            if trace_token is not None:
                trace_id_var.reset(trace_token)

    record_parse_latency(
        provider=extractor.provider_name, stage="total", seconds=time.perf_counter() - total_start
    )
    record_parse_request(
        provider=extractor.provider_name, model=extractor.model_name, status=status
    )

    return ParseResponse(
        status=status, extraction=extraction, trace_id=trace_id, entitlement=entitlement
    )


async def _to_response(
    outcome: ExtractionResult,
    *,
    db: AsyncSession,
    user: User,
    known_categories: list[str],
    reference_time: datetime,
) -> tuple[str, ExtractionOut | None]:
    settings = get_settings()
    if outcome.raw is None:
        return "failed", None

    if outcome.raw.confidence >= settings.CONFIDENCE_OK_THRESHOLD:
        status = "ok"
    elif outcome.raw.confidence >= settings.CONFIDENCE_LOW_THRESHOLD:
        status = "low_confidence"
    else:
        return "failed", None

    extraction = await _build_extraction_out(
        outcome.raw,
        db=db,
        user=user,
        known_categories=known_categories,
        reference_time=reference_time,
    )
    return status, extraction


@router.post("/parse", response_model=ParseResponse)
async def parse_expense(
    payload: ParseRequest,
    user: Annotated[User, Depends(get_current_db_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    extractor: Annotated[LLMExtractor, Depends(get_extractor)],
    _rate_limit: Annotated[None, Depends(enforce_parse_rate_limit)],
) -> ParseResponse:
    settings = get_settings()
    if len(payload.text) > settings.PARSE_MAX_TEXT_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"text exceeds the {settings.PARSE_MAX_TEXT_LENGTH}-character limit",
        )

    # Quota gates the paid LLM call only — never the sync endpoint. Pro
    # users always get quota=None/over_quota=False from the signal, so this
    # never gates them regardless of how much they've logged.
    entitlement = await build_entitlement_signal(db, user)
    if entitlement.over_quota:
        record_parse_request(
            provider=extractor.provider_name, model=extractor.model_name, status="quota_exceeded"
        )
        return ParseResponse(
            status="quota_exceeded", extraction=None, trace_id=None, entitlement=entitlement
        )

    reference_time = payload.reference_time or datetime.now(UTC)
    return await _run_extraction(
        text=payload.text,
        user=user,
        db=db,
        reference_time=reference_time,
        extractor=extractor,
        entitlement=entitlement,
    )


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_expense(
    user: Annotated[User, Depends(get_current_db_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    extractor: Annotated[LLMExtractor, Depends(get_extractor)],
    stt: Annotated[STTProvider, Depends(get_stt_provider)],
    audio: Annotated[UploadFile, File()],
    _rate_limit: Annotated[None, Depends(enforce_parse_rate_limit)],
    reference_time: Annotated[datetime | None, Form()] = None,
) -> TranscribeResponse:
    settings = get_settings()

    # Read once into an app-owned `bytes` object and close the upload
    # immediately — nothing past this point ever writes `audio_bytes`
    # anywhere (no disk, DB, StorageBackend, or log call touches it; see
    # the module docstring). Note: like any multipart parser, the ASGI
    # layer may transiently spool very large uploads through its own
    # temp storage while *receiving* the request, before our code ever
    # sees it — that's outside application code's control, is discarded
    # the moment the request completes, and is unrelated to the guarantee
    # we own here: this handler never persists the audio itself.
    audio_bytes = await audio.read()
    await audio.close()
    if len(audio_bytes) > settings.MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"audio exceeds the {settings.MAX_AUDIO_BYTES}-byte limit",
        )

    mime_type = audio.content_type or "application/octet-stream"

    # Gate the whole paid pipeline BEFORE spending on STT — a free user over
    # quota shouldn't cost us a transcription call just to then discard its
    # output for the LLM step. transcript is empty since STT never ran.
    entitlement = await build_entitlement_signal(db, user)
    if entitlement.over_quota:
        del audio_bytes
        record_parse_request(
            provider=extractor.provider_name, model=extractor.model_name, status="quota_exceeded"
        )
        return TranscribeResponse(
            status="quota_exceeded",
            extraction=None,
            trace_id=None,
            entitlement=entitlement,
            transcript="",
        )

    stt_start = time.perf_counter()
    with traced_operation(
        "transcribe_audio", as_type="generation", model=stt.model_name
    ) as stt_span:
        try:
            transcript = await asyncio.wait_for(
                stt.transcribe(audio_bytes, mime_type), timeout=settings.PARSE_TIMEOUT_S * 3
            )
        except Exception as exc:  # noqa: BLE001 - STT failure -> failed, not 500
            stt_span.update(metadata={"provider": stt.provider_name, "error": str(exc)})
            record_parse_latency(
                provider=stt.provider_name, stage="stt", seconds=time.perf_counter() - stt_start
            )
            record_parse_request(
                provider=extractor.provider_name, model=extractor.model_name, status="failed"
            )
            return TranscribeResponse(
                status="failed",
                extraction=None,
                trace_id=current_trace_id(),
                transcript="",
                entitlement=entitlement,
            )

        record_parse_latency(
            provider=stt.provider_name, stage="stt", seconds=time.perf_counter() - stt_start
        )
        stt_span.update(
            # Transcript TEXT only, gated by config — audio bytes are never
            # passed to Langfuse or anywhere else.
            output={"text": transcript.text} if settings.LANGFUSE_CAPTURE_TRANSCRIPT else None,
            metadata={"provider": stt.provider_name},
        )

    del audio_bytes  # explicit: nothing below this line should ever reference it again

    if settings.LOG_CAPTURE_TRANSCRIPT:
        logger.info(
            "transcription completed",
            extra={"provider": stt.provider_name, "transcript": transcript.text},
        )

    resolved_reference_time = reference_time or datetime.now(UTC)
    parse_result = await _run_extraction(
        text=transcript.text,
        user=user,
        db=db,
        reference_time=resolved_reference_time,
        extractor=extractor,
        entitlement=entitlement,
    )
    return TranscribeResponse(**parse_result.model_dump(), transcript=transcript.text)
