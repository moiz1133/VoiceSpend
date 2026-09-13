"""Tests for Phase 5 quota gating on POST /api/v1/parse and /transcribe.

Quota gates the paid LLM/STT calls ONLY — never /api/v1/sync (see
tests/test_metering.py for that guarantee). These tests assert the
extractor/STT ABC is never invoked at all when a free user is over quota,
not just that the response looks a certain way.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_db_user
from app.core.security import AuthUser, get_current_user
from app.db.session import get_db
from app.main import create_app
from app.models import Entitlement, UsageCounter, User
from app.schemas.enums import EntitlementStatus, Tier
from app.services.extraction.base import ExtractionContext, ExtractionResult, LLMExtractor
from app.services.extraction.factory import get_extractor
from app.services.extraction.schema import RawExtraction
from app.services.metering.usage import current_period
from app.services.stt.base import STTProvider, Transcript
from app.services.stt.factory import get_stt_provider

USER_SUB = "supabase|quota-user"


class CountingExtractor(LLMExtractor):
    provider_name = "fake"
    model_name = "fake-model"

    def __init__(self) -> None:
        self.call_count = 0

    async def extract(self, text: str, ctx: ExtractionContext) -> ExtractionResult:
        self.call_count += 1
        return ExtractionResult(
            raw=RawExtraction(amount=10, currency="USD", confidence=0.9),
            prompt_tokens=1,
            completion_tokens=1,
        )


class CountingSTT(STTProvider):
    provider_name = "fake"
    model_name = "fake-stt-model"

    def __init__(self) -> None:
        self.call_count = 0

    async def transcribe(self, audio: bytes, mime_type: str) -> Transcript:
        self.call_count += 1
        return Transcript(text="ten dollars")


async def _provision(db_session: AsyncSession, sub: str) -> User:
    return await get_current_db_user(AuthUser(id=sub, email=None, role=None), db_session)


async def _set_usage(db_session: AsyncSession, user_id: UUID, count: int) -> None:
    db_session.add(UsageCounter(user_id=user_id, period=current_period(), log_count=count))
    await db_session.commit()


async def _make_pro(db_session: AsyncSession, user_id: UUID) -> None:
    db_session.add(
        Entitlement(
            user_id=user_id,
            tier=Tier.PRO,
            status=EntitlementStatus.ACTIVE,
            current_period_end=datetime.now(UTC) + timedelta(days=30),
        )
    )
    await db_session.commit()


def _client(
    db_session: AsyncSession,
    sub: str,
    *,
    extractor: LLMExtractor,
    stt: STTProvider | None = None,
) -> AsyncClient:
    app = create_app()

    async def _override_db():
        yield db_session

    async def _override_user() -> AuthUser:
        return AuthUser(id=sub, email=None, role=None)

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_extractor] = lambda: extractor
    if stt is not None:
        app.dependency_overrides[get_stt_provider] = lambda: stt
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_free_user_under_quota_calls_llm(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_SUB)
    await _set_usage(db_session, user.id, 5)
    extractor = CountingExtractor()

    async with _client(db_session, USER_SUB, extractor=extractor) as client:
        response = await client.post("/api/v1/parse", json={"text": "ten dollars for coffee"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert extractor.call_count == 1
    assert body["entitlement"]["used"] == 5
    assert body["entitlement"]["over_quota"] is False


async def test_free_user_at_quota_blocks_llm(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_SUB)
    await _set_usage(db_session, user.id, 20)  # == FREE_MONTHLY_LOG_QUOTA default
    extractor = CountingExtractor()

    async with _client(db_session, USER_SUB, extractor=extractor) as client:
        response = await client.post("/api/v1/parse", json={"text": "ten dollars for coffee"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "quota_exceeded"
    assert body["extraction"] is None
    assert extractor.call_count == 0
    assert body["entitlement"]["over_quota"] is True


async def test_pro_user_over_high_count_still_calls_llm(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_SUB)
    await _make_pro(db_session, user.id)
    await _set_usage(db_session, user.id, 500)
    extractor = CountingExtractor()

    async with _client(db_session, USER_SUB, extractor=extractor) as client:
        response = await client.post("/api/v1/parse", json={"text": "ten dollars for coffee"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert extractor.call_count == 1
    assert body["entitlement"]["quota"] is None
    assert body["entitlement"]["over_quota"] is False


async def test_free_user_at_quota_blocks_transcribe_before_stt(db_session: AsyncSession) -> None:
    user = await _provision(db_session, USER_SUB)
    await _set_usage(db_session, user.id, 20)
    extractor = CountingExtractor()
    stt = CountingSTT()

    async with _client(db_session, USER_SUB, extractor=extractor, stt=stt) as client:
        response = await client.post(
            "/api/v1/transcribe", files={"audio": ("clip.wav", b"fake-audio", "audio/wav")}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "quota_exceeded"
    assert body["transcript"] == ""
    assert stt.call_count == 0
    assert extractor.call_count == 0
