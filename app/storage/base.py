"""Abstract storage interface.

Nothing in the app depends on Supabase Storage directly — everything goes
through this interface so the backing implementation (Supabase today, R2/S3
later) can be swapped without touching callers. Not wired into any endpoint
yet; this is Phase 0 scaffolding only.
"""

from abc import ABC, abstractmethod


class StorageBackend(ABC):
    @abstractmethod
    async def put(self, path: str, data: bytes, content_type: str | None = None) -> None:
        """Upload/overwrite an object at `path`."""

    @abstractmethod
    async def get(self, path: str) -> bytes:
        """Download the object at `path`."""

    @abstractmethod
    async def delete(self, path: str) -> None:
        """Delete the object at `path`."""

    @abstractmethod
    async def presigned_url(self, path: str, expires_in: int = 3600) -> str:
        """Return a time-limited URL for downloading `path`."""
