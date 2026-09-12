"""The only place in the codebase allowed to know which STT provider is
configured. Mirrors app/services/extraction/factory.py.
"""

from app.core.config import get_settings
from app.services.stt.base import STTProvider


def get_stt_provider() -> STTProvider:
    settings = get_settings()

    if settings.STT_PROVIDER == "groq":
        from app.services.stt.groq_whisper import GroqWhisperSTT

        return GroqWhisperSTT()

    if settings.STT_PROVIDER == "deepgram":
        from app.services.stt.deepgram import DeepgramSTT

        return DeepgramSTT()

    raise ValueError(f"Unknown STT_PROVIDER: {settings.STT_PROVIDER!r}")
