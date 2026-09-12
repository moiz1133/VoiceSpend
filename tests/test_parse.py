"""Tests for POST /api/v1/parse and POST /api/v1/transcribe.

All external providers are mocked at the LLMExtractor / STTProvider ABC
boundary — see the module docstrings in app/services/extraction/anthropic.py
and gemini.py for why that's the right mocking boundary (not the vendor
SDKs themselves): the endpoint and factory wiring is what's under test,
not any particular vendor's current API shape. No live provider calls are
made anywhere in this file.
"""

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_db_user
from app.core.config import get_settings
from app.core.security import AuthUser, get_current_user
from app.db.session import get_db
from app.main import create_app
from app.models import User
from app.services.extraction.base import ExtractionContext, ExtractionResult, LLMExtractor
from app.services.extraction.factory import get_extractor
from app.services.extraction.schema import RawExtraction
from app.services.stt.base import STTProvider, Transcript
from app.services.stt.factory import get_stt_provider

USER_SUB = "supabase|parse-user"


class FakeExtractor(LLMExtractor):
    provider_name = "fake"
    model_name = "fake-model"

    def __init__(
        self,
        raw: RawExtraction | None = None,
        *,
        error: str | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._raw = raw
        self._error = error
        self._raises = raises

    async def extract(self, text: str, ctx: ExtractionContext) -> ExtractionResult:
        if self._raises is not None:
            raise self._raises
        return ExtractionResult(
            raw=self._raw, error=self._error, prompt_tokens=10, completion_tokens=5
        )


class DisambiguatingFakeExtractor(LLMExtractor):
    """Stands in for the LLM's currency-disambiguation prompt rule: when
    the utterance is ambiguous, fall back to the caller's base_currency.
    Tests against this verify OUR context-threading (base_currency reaches
    the extractor), which is what the app controls — not a real LLM's
    linguistic judgment, which can't be asserted on without a live call.
    """

    provider_name = "fake"
    model_name = "fake-model"

    async def extract(self, text: str, ctx: ExtractionContext) -> ExtractionResult:
        currency = ctx.base_currency if "rupee" in text.lower() else "USD"
        return ExtractionResult(raw=RawExtraction(amount=2000, currency=currency, confidence=0.9))


class FakeSTT(STTProvider):
    provider_name = "fake"
    model_name = "fake-stt-model"

    def __init__(self, text: str = "transcribed text", *, raises: Exception | None = None) -> None:
        self._text = text
        self._raises = raises

    async def transcribe(self, audio: bytes, mime_type: str) -> Transcript:
        if self._raises is not None:
            raise self._raises
        return Transcript(text=self._text)


async def _provision(db_session: AsyncSession, sub: str, *, base_currency: str = "USD") -> User:
    user = await get_current_db_user(AuthUser(id=sub, email=None, role=None), db_session)
    if user.base_currency != base_currency:
        user.base_currency = base_currency
        db_session.add(user)
        await db_session.commit()
    return user


def _client(
    db_session: AsyncSession,
    sub: str,
    *,
    extractor: LLMExtractor | None = None,
    stt: STTProvider | None = None,
    authenticated: bool = True,
) -> AsyncClient:
    app = create_app()

    async def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    if authenticated:

        async def _override_user() -> AuthUser:
            return AuthUser(id=sub, email=None, role=None)

        app.dependency_overrides[get_current_user] = _override_user
    if extractor is not None:
        app.dependency_overrides[get_extractor] = lambda: extractor
    if stt is not None:
        app.dependency_overrides[get_stt_provider] = lambda: stt
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_parse_happy_path(db_session: AsyncSession) -> None:
    await _provision(db_session, USER_SUB, base_currency="USD")
    raw = RawExtraction(
        amount=45.0,
        currency="USD",
        category="Groceries",
        payment_method="card",
        merchant="Walmart",
        spent_at=None,
        confidence=0.9,
    )

    async with _client(db_session, USER_SUB, extractor=FakeExtractor(raw=raw)) as client:
        response = await client.post(
            "/api/v1/parse", json={"text": "Bought groceries for $45 at Walmart"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    extraction = body["extraction"]
    assert Decimal(str(extraction["amount_original"])) == Decimal("45.0")
    assert extraction["currency_original"] == "USD"
    assert extraction["category"] == "Groceries"
    assert extraction["category_is_known"] is True
    assert extraction["payment_method"] == "card"
    assert extraction["merchant"] == "Walmart"


@pytest.mark.parametrize("base_currency", ["PKR", "INR"])
async def test_currency_disambiguation_uses_base_currency(
    db_session: AsyncSession, base_currency: str
) -> None:
    sub = f"supabase|disambig-{base_currency}"
    await _provision(db_session, sub, base_currency=base_currency)

    async with _client(db_session, sub, extractor=DisambiguatingFakeExtractor()) as client:
        response = await client.post(
            "/api/v1/parse", json={"text": "Paid 2000 rupees in cash for dinner"}
        )

    assert response.json()["extraction"]["currency_original"] == base_currency


async def test_category_known_name_is_canonicalized(db_session: AsyncSession) -> None:
    await _provision(db_session, USER_SUB)
    # Lowercase, doesn't match the seeded "Food" casing exactly.
    raw = RawExtraction(amount=10, currency="USD", category="food", confidence=0.9)

    async with _client(db_session, USER_SUB, extractor=FakeExtractor(raw=raw)) as client:
        response = await client.post("/api/v1/parse", json={"text": "coffee"})

    extraction = response.json()["extraction"]
    assert extraction["category"] == "Food"
    assert extraction["category_is_known"] is True


async def test_category_novel_name_kept_as_free_text(db_session: AsyncSession) -> None:
    await _provision(db_session, USER_SUB)
    raw = RawExtraction(amount=10, currency="USD", category="Pet Supplies", confidence=0.9)

    async with _client(db_session, USER_SUB, extractor=FakeExtractor(raw=raw)) as client:
        response = await client.post("/api/v1/parse", json={"text": "dog food"})

    extraction = response.json()["extraction"]
    assert extraction["category"] == "Pet Supplies"
    assert extraction["category_is_known"] is False


async def test_parse_over_length_rejected(db_session: AsyncSession) -> None:
    await _provision(db_session, USER_SUB)
    settings = get_settings()
    too_long = "a" * (settings.PARSE_MAX_TEXT_LENGTH + 1)

    async with _client(db_session, USER_SUB, extractor=FakeExtractor()) as client:
        response = await client.post("/api/v1/parse", json={"text": too_long})

    assert response.status_code == 422


async def test_provider_error_returns_failed_not_500(db_session: AsyncSession) -> None:
    await _provision(db_session, USER_SUB)
    extractor = FakeExtractor(raises=TimeoutError("provider timed out"))

    async with _client(db_session, USER_SUB, extractor=extractor) as client:
        response = await client.post("/api/v1/parse", json={"text": "coffee 5 dollars"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["extraction"] is None


async def test_malformed_llm_output_returns_failed_not_500(db_session: AsyncSession) -> None:
    """Simulates a provider returning content that didn't validate against
    RawExtraction — app/services/extraction/anthropic.py and gemini.py both
    catch that at the provider layer and return this same raw=None/error
    shape; the endpoint's handling of it is what's under test here.
    """
    await _provision(db_session, USER_SUB)
    extractor = FakeExtractor(raw=None, error="malformed extraction output: junk")

    async with _client(db_session, USER_SUB, extractor=extractor) as client:
        response = await client.post("/api/v1/parse", json={"text": "something"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["extraction"] is None


async def test_low_confidence_status(db_session: AsyncSession) -> None:
    await _provision(db_session, USER_SUB)
    settings = get_settings()
    mid_confidence = (settings.CONFIDENCE_LOW_THRESHOLD + settings.CONFIDENCE_OK_THRESHOLD) / 2
    raw = RawExtraction(amount=10, currency="USD", confidence=mid_confidence)

    async with _client(db_session, USER_SUB, extractor=FakeExtractor(raw=raw)) as client:
        response = await client.post("/api/v1/parse", json={"text": "something vague"})

    body = response.json()
    assert body["status"] == "low_confidence"
    assert body["extraction"] is not None  # best-effort fields still returned


async def test_parse_requires_auth(db_session: AsyncSession) -> None:
    async with _client(
        db_session, USER_SUB, extractor=FakeExtractor(), authenticated=False
    ) as client:
        response = await client.post("/api/v1/parse", json={"text": "coffee"})

    assert response.status_code == 401


async def test_transcribe_happy_path_runs_extraction(db_session: AsyncSession) -> None:
    await _provision(db_session, USER_SUB)
    raw = RawExtraction(amount=12, currency="USD", category="Transport", confidence=0.9)

    async with _client(
        db_session,
        USER_SUB,
        extractor=FakeExtractor(raw=raw),
        stt=FakeSTT(text="uber ride cost twelve dollars"),
    ) as client:
        response = await client.post(
            "/api/v1/transcribe",
            files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["transcript"] == "uber ride cost twelve dollars"
    assert body["status"] == "ok"
    assert body["extraction"]["category"] == "Transport"


async def test_transcribe_never_persists_audio_to_disk(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audio payload here is small enough to stay entirely in memory
    (well under any multipart spool threshold), so patching `open` proves
    neither our handler nor the small-file path writes it to disk.
    """

    def _forbidden_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("audio bytes must never be written to disk")

    monkeypatch.setattr("builtins.open", _forbidden_open)

    await _provision(db_session, USER_SUB)
    raw = RawExtraction(amount=1, currency="USD", confidence=0.9)

    async with _client(
        db_session, USER_SUB, extractor=FakeExtractor(raw=raw), stt=FakeSTT()
    ) as client:
        response = await client.post(
            "/api/v1/transcribe",
            files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
        )

    assert response.status_code == 200


async def test_transcribe_over_size_rejected(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_AUDIO_BYTES", "100")
    get_settings.cache_clear()
    try:
        await _provision(db_session, USER_SUB)
        async with _client(
            db_session, USER_SUB, extractor=FakeExtractor(), stt=FakeSTT()
        ) as client:
            response = await client.post(
                "/api/v1/transcribe",
                files={"audio": ("clip.wav", b"x" * 200, "audio/wav")},
            )
        assert response.status_code == 413
    finally:
        get_settings.cache_clear()


async def test_transcribe_stt_error_returns_failed_not_500(db_session: AsyncSession) -> None:
    await _provision(db_session, USER_SUB)

    async with _client(
        db_session,
        USER_SUB,
        extractor=FakeExtractor(),
        stt=FakeSTT(raises=RuntimeError("stt provider down")),
    ) as client:
        response = await client.post(
            "/api/v1/transcribe",
            files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["extraction"] is None


async def test_transcribe_requires_auth(db_session: AsyncSession) -> None:
    async with _client(
        db_session, USER_SUB, extractor=FakeExtractor(), stt=FakeSTT(), authenticated=False
    ) as client:
        response = await client.post(
            "/api/v1/transcribe",
            files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
        )

    assert response.status_code == 401
