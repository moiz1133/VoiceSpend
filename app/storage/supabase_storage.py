"""Supabase Storage implementation of the StorageBackend interface."""

from storage3.types import FileOptions
from supabase import AsyncClient, create_async_client

from app.core.config import Settings, get_settings
from app.storage.base import StorageBackend


class SupabaseStorageBackend(StorageBackend):
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: AsyncClient | None = None

    async def _get_client(self) -> AsyncClient:
        if self._client is None:
            self._client = await create_async_client(
                self._settings.SUPABASE_URL,
                self._settings.SUPABASE_SERVICE_KEY,
            )
        return self._client

    async def put(self, path: str, data: bytes, content_type: str | None = None) -> None:
        client = await self._get_client()
        bucket = client.storage.from_(self._settings.SUPABASE_STORAGE_BUCKET)
        file_options: FileOptions = {"upsert": "true"}
        if content_type:
            file_options["content-type"] = content_type
        await bucket.upload(path, data, file_options)

    async def get(self, path: str) -> bytes:
        client = await self._get_client()
        bucket = client.storage.from_(self._settings.SUPABASE_STORAGE_BUCKET)
        return await bucket.download(path)

    async def delete(self, path: str) -> None:
        client = await self._get_client()
        bucket = client.storage.from_(self._settings.SUPABASE_STORAGE_BUCKET)
        await bucket.remove([path])

    async def presigned_url(self, path: str, expires_in: int = 3600) -> str:
        client = await self._get_client()
        bucket = client.storage.from_(self._settings.SUPABASE_STORAGE_BUCKET)
        result = await bucket.create_signed_url(path, expires_in)
        return str(result["signedURL"])
