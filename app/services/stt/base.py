"""The provider-agnostic speech-to-text interface — mirrors
app/services/extraction/base.py's shape for the same reason: nothing
outside factory.py should know which STT vendor is configured.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Transcript:
    text: str
    language: str | None = None


class STTProvider(ABC):
    provider_name: str
    model_name: str

    @abstractmethod
    async def transcribe(self, audio: bytes, mime_type: str) -> Transcript:
        """Transcribe in-memory audio bytes. Must not persist `audio`
        anywhere — not to disk, DB, logs, or object storage.
        """
        raise NotImplementedError
