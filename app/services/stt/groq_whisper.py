"""Groq-hosted Whisper implementation of STTProvider.

Only loaded via app/services/stt/factory.py — see the note at the top of
app/services/extraction/anthropic.py; the same reasoning applies here
(tests mock the STTProvider ABC, not this SDK call).
"""

from groq import AsyncGroq

from app.core.config import get_settings
from app.services.stt.base import STTProvider, Transcript


class GroqWhisperSTT(STTProvider):
    provider_name = "groq"

    def __init__(self) -> None:
        settings = get_settings()
        self.model_name = settings.STT_MODEL
        self._client = AsyncGroq(api_key=settings.GROQ_API_KEY)

    async def transcribe(self, audio: bytes, mime_type: str) -> Transcript:
        extension = mime_type.split("/")[-1] or "m4a"
        response = await self._client.audio.transcriptions.create(
            model=self.model_name,
            file=(f"audio.{extension}", audio, mime_type),
            response_format="json",
        )
        return Transcript(text=response.text)
