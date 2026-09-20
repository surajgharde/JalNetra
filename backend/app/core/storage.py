"""Thin object-storage client (MinIO / S3-compatible). Rasters and cached arrays
live here; the database only ever stores their keys."""

from __future__ import annotations

import io
from functools import lru_cache
from typing import Protocol

from minio import Minio
from minio.error import S3Error

from app.core.config import Settings, get_settings


class ObjectStore(Protocol):
    def put_bytes(self, key: str, data: bytes, content_type: str = ...) -> str: ...
    def get_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


class MinioStore:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.minio_bucket
        self._client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )

    def ensure_bucket(self) -> None:
        if not self._client.bucket_exists(self.bucket):
            self._client.make_bucket(self.bucket)

    def put_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        self._client.put_object(
            self.bucket, key, io.BytesIO(data), length=len(data), content_type=content_type
        )
        return key

    def get_bytes(self, key: str) -> bytes:
        resp = self._client.get_object(self.bucket, key)
        try:
            return bytes(resp.read())
        finally:
            resp.close()
            resp.release_conn()

    def exists(self, key: str) -> bool:
        try:
            self._client.stat_object(self.bucket, key)
            return True
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject", "NoSuchBucket"):
                return False
            raise

    def delete(self, key: str) -> None:
        self._client.remove_object(self.bucket, key)


class MemoryStore:
    """In-memory ObjectStore for tests."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        self.objects[key] = data
        return key

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


@lru_cache
def get_store() -> MinioStore:
    store = MinioStore(get_settings())
    store.ensure_bucket()
    return store
