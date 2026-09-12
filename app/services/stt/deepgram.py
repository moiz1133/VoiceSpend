"""Deepgram implementation of STTProvider.

Only loaded via app/services/stt/factory.py — see the note at the top of
app/services/extraction/anthropic.py; tests mock the STTProvider ABC, not
this SDK call.
"""

from deepgram import AsyncDeepgramClient

from app.core.config import get_settings
from app.services.stt.base import STTProvider, Transcript


class DeepgramSTT(STTProvider):
    provider_name = "deepgram"

    def __init__(self) -> None:
        settings = get_settings()
        self.model_name = settings.STT_MODEL
        self._client = AsyncDeepgramClient(api_key=settings.DEEPGRAM_API_KEY)

    async def transcribe(self, audio: bytes, mime_type: str) -> Transcript:
        # Deepgram's file-transcription call takes raw bytes and doesn't
        # accept an explicit content-type at this layer; `mime_type` is
        # kept on the interface for providers (like Groq) that do need it.
        response = await self._client.listen.v1.media.transcribe_file(
            request=audio, model=self.model_name, smart_format=True
        )
        channels = response.results.channels if response.results else []
        alternatives = channels[0].alternatives if channels and channels[0].alternatives else []
        text = alternatives[0].transcript if alternatives else ""
        return Transcript(text=text)
