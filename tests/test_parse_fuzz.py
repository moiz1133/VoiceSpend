"""Fuzz coverage for the extraction validation boundary — the other bug
nest alongside sync idempotency.

`_ValidatingFakeExtractor` reproduces exactly what app/services/extraction/
anthropic.py and gemini.py do for real: take whatever the "LLM" returned
and validate it against RawExtraction, catching a ValidationError into
ExtractionResult(raw=None, error=...) rather than raising. That's the
real boundary worth fuzzing — not the vendor SDK call shape, which
tests/test_parse.py's mocking-at-the-ABC already established is out of
scope for this test suite.

Every scenario here must resolve to a 200 with a well-formed body — NEVER
a 500 — regardless of how malformed the "LLM" output was.
"""

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import AuthUser, get_current_user
from app.db.session import get_db
from app.main import create_app
from app.services.extraction.base import ExtractionContext, ExtractionResult, LLMExtractor
from app.services.extraction.factory import get_extractor
from app.services.extraction.schema import RawExtraction

pytestmark = pytest.mark.pg

USER_SUB = "supabase|fuzz-user"


class _ValidatingFakeExtractor(LLMExtractor):
    provider_name = "fake"
    model_name = "fake-model"

    def __init__(self, raw_payload: Any) -> None:
        self._raw_payload = raw_payload

    async def extract(self, text: str, ctx: ExtractionContext) -> ExtractionResult:
        try:
            raw = RawExtraction.model_validate(self._raw_payload)
        except Exception as exc:  # noqa: BLE001 - exactly what the real providers do
            return ExtractionResult(raw=None, error=f"malformed extraction output: {exc}")
        return ExtractionResult(raw=raw, prompt_tokens=10, completion_tokens=5)


def _client(db_session: AsyncSession, extractor: LLMExtractor) -> AsyncClient:
    app = create_app()

    async def _override_db():
        yield db_session

    async def _override_user() -> AuthUser:
        return AuthUser(id=USER_SUB, email=None, role=None)

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_extractor] = lambda: extractor
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _provision(db_session: AsyncSession) -> None:
    from app.api.deps import get_current_db_user

    await get_current_db_user(AuthUser(id=USER_SUB, email=None, role=None), db_session)


_MALFORMED_SCENARIOS: dict[str, tuple[Any, str]] = {
    # scenario -> (raw payload, expected status)
    "empty_object": ({}, "failed"),  # confidence is required
    "missing_confidence": ({"amount": 10, "currency": "USD"}, "failed"),
    "null_everything_but_confidence": (
        {
            "amount": None,
            "currency": None,
            "category": None,
            "payment_method": None,
            "merchant": None,
            "spent_at": None,
            "confidence": 0.5,
        },
        # Valid (if useless) extraction — "coerce where safe," not a
        # failure: every field is genuinely Optional except confidence.
        "low_confidence",
    ),
    "amount_as_non_numeric_string": (
        {"amount": "forty five", "currency": "USD", "confidence": 0.9}, "failed"
    ),
    "confidence_out_of_range": ({"amount": 10, "currency": "USD", "confidence": 1.5}, "failed"),
    "negative_amount": ({"amount": -50, "currency": "USD", "confidence": 0.9}, "failed"),
    "non_json_junk": ("this is not json at all, just garbage text {{{", "failed"),
    "tool_call_returns_nothing": (None, "failed"),
    "confidence_wrong_type": (
        {"amount": 10, "currency": "USD", "confidence": "high"}, "failed"
    ),
    "deeply_wrong_shape": (["not", "even", "a", "dict"], "failed"),
}


@pytest.mark.parametrize("scenario", sorted(_MALFORMED_SCENARIOS))
async def test_malformed_llm_output_never_500s(
    db_session: AsyncSession, scenario: str
) -> None:
    await _provision(db_session)
    raw_payload, expected_status = _MALFORMED_SCENARIOS[scenario]
    extractor = _ValidatingFakeExtractor(raw_payload)

    async with _client(db_session, extractor) as client:
        response = await client.post("/api/v1/parse", json={"text": "some spoken text"})

    assert response.status_code == 200, scenario
    body = response.json()
    assert body["status"] in ("ok", "low_confidence", "failed", "quota_exceeded"), scenario
    assert body["status"] == expected_status, scenario
    if expected_status == "failed":
        assert body["extraction"] is None, scenario


async def test_extra_unknown_fields_are_ignored_not_rejected(db_session: AsyncSession) -> None:
    """Pydantic's default (non-strict) model config ignores unrecognized
    keys — an LLM tool-call payload gaining an extra field over time (a
    new provider version, a hallucinated key) must not break extraction.
    """
    await _provision(db_session)
    payload = {
        "amount": 12.5,
        "currency": "USD",
        "confidence": 0.9,
        "some_field_the_llm_made_up": "whatever",
    }
    extractor = _ValidatingFakeExtractor(payload)

    async with _client(db_session, extractor) as client:
        response = await client.post("/api/v1/parse", json={"text": "coffee"})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_numeric_string_amount_coerces_successfully(db_session: AsyncSession) -> None:
    """Pydantic v2's lax mode coerces a numeric string to float — this is
    "coerce where safe," not a failure.
    """
    await _provision(db_session)
    payload = {"amount": "45.00", "currency": "USD", "confidence": 0.9}
    extractor = _ValidatingFakeExtractor(payload)

    async with _client(db_session, extractor) as client:
        response = await client.post("/api/v1/parse", json={"text": "coffee for $45"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert float(body["extraction"]["amount_original"]) == 45.0


async def test_huge_number_does_not_crash(db_session: AsyncSession) -> None:
    await _provision(db_session)
    payload = {"amount": 1e18, "currency": "USD", "confidence": 0.9}
    extractor = _ValidatingFakeExtractor(payload)

    async with _client(db_session, extractor) as client:
        response = await client.post("/api/v1/parse", json={"text": "a very large purchase"})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize(
    ("raw_currency", "expected"),
    [
        ("usd", "USD"),  # lowercase -> normalized
        (" usd ", "USD"),  # whitespace + lowercase -> normalized
        ("USDX", None),  # 4 chars -> rejected, not truncated/guessed
        ("US", None),  # 2 chars -> rejected
        ("12A", None),  # non-alphabetic -> rejected
    ],
)
async def test_currency_post_validation_normalizes_or_rejects(
    db_session: AsyncSession, raw_currency: str, expected: str | None
) -> None:
    await _provision(db_session)
    payload = {"amount": 10, "currency": raw_currency, "confidence": 0.9}
    extractor = _ValidatingFakeExtractor(payload)

    async with _client(db_session, extractor) as client:
        response = await client.post("/api/v1/parse", json={"text": "something"})

    assert response.status_code == 200
    extraction = response.json()["extraction"]
    assert extraction["currency_original"] == expected
    if expected is not None:
        assert len(expected) == 3
        assert expected.isupper()
    # An unresolvable currency must never produce a best-effort amount_base.
    if expected is None:
        assert extraction["amount_base"] is None


async def test_unknown_category_kept_as_free_text_not_a_failure(db_session: AsyncSession) -> None:
    await _provision(db_session)
    payload = {
        "amount": 10,
        "currency": "USD",
        "category": "Alien Spacecraft Fuel",
        "confidence": 0.9,
    }
    extractor = _ValidatingFakeExtractor(payload)

    async with _client(db_session, extractor) as client:
        response = await client.post("/api/v1/parse", json={"text": "fuel"})

    assert response.status_code == 200
    extraction = response.json()["extraction"]
    assert extraction["category"] == "Alien Spacecraft Fuel"
    assert extraction["category_is_known"] is False


async def test_injected_control_chars_do_not_crash_json_encoding(db_session: AsyncSession) -> None:
    payload = {
        "amount": 10,
        "currency": "USD",
        "merchant": "Evil\x00Corp\x1b[31mRed\x07",
        "confidence": 0.9,
    }
    await _provision(db_session)
    extractor = _ValidatingFakeExtractor(payload)

    async with _client(db_session, extractor) as client:
        response = await client.post("/api/v1/parse", json={"text": "something"})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_transcribe_audio_not_persisted_holds_under_malformed_output(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audio-never-touches-disk invariant (app/api/v1/parse.py) must
    hold regardless of how the extraction step turns out — fuzz it with a
    malformed payload rather than the happy-path RawExtraction the
    existing transcribe tests use.
    """
    from app.services.stt.base import STTProvider, Transcript
    from app.services.stt.factory import get_stt_provider

    class _FakeSTT(STTProvider):
        provider_name = "fake"
        model_name = "fake-stt-model"

        async def transcribe(self, audio: bytes, mime_type: str) -> Transcript:
            return Transcript(text="garbled nonsense audio transcript")

    def _forbidden_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("audio bytes must never be written to disk")

    monkeypatch.setattr("builtins.open", _forbidden_open)

    await _provision(db_session)
    extractor = _ValidatingFakeExtractor("not json at all")

    app = create_app()

    async def _override_db():
        yield db_session

    async def _override_user() -> AuthUser:
        return AuthUser(id=USER_SUB, email=None, role=None)

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_extractor] = lambda: extractor
    app.dependency_overrides[get_stt_provider] = lambda: _FakeSTT()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/transcribe",
            files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
