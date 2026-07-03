"""StorageClient abstraction: S3/MinIO in Compose, local-FS fallback for bare dev.

Methods are synchronous (boto3 is sync); async callers wrap them in
``asyncio.to_thread``. ``file_ref`` values persisted on ``resumes`` are the object
key, so erasure can delete precisely by key/prefix.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Protocol

from .settings import Settings


class StorageClient(Protocol):
    def put_bytes(self, key: str, data: bytes, content_type: str) -> str: ...
    def delete(self, key: str) -> None: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def get_bytes(self, key: str) -> bytes: ...


class LocalStorageClient:
    """Filesystem-backed store rooted at a base directory."""

    def __init__(self, base_dir: str) -> None:
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.base / key).resolve()
        if not str(p).startswith(str(self.base.resolve())):
            raise ValueError("path traversal detected in storage key")
        return p

    def put_bytes(self, key: str, data: bytes, content_type: str) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def delete_prefix(self, prefix: str) -> int:
        root = self._path(prefix)
        if root.exists() and root.is_dir():
            count = sum(1 for _ in root.rglob("*") if _.is_file())
            shutil.rmtree(root)
            return count
        return 0

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()


class S3StorageClient:
    """S3-compatible store (AWS S3 or MinIO via endpoint override)."""

    def __init__(self, settings: Settings) -> None:
        import boto3

        self.bucket = settings.storage_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url or None,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        from botocore.exceptions import ClientError

        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self.bucket)

    def put_bytes(self, key: str, data: bytes, content_type: str) -> str:
        self._client.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
        )
        return key

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def delete_prefix(self, prefix: str) -> int:
        paginator = self._client.get_paginator("list_objects_v2")
        deleted = 0
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objs:
                self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": objs})
                deleted += len(objs)
        return deleted

    def get_bytes(self, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"].read()


def get_storage(settings: Settings) -> StorageClient:
    backend = (settings.storage_backend or "s3").lower()
    if backend == "local":
        return LocalStorageClient(settings.storage_local_dir)
    return S3StorageClient(settings)


def resume_key(user_id, resume_id, filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower() or ".bin"
    return f"resumes/{user_id}/{resume_id}{ext}"


def user_prefix(user_id) -> str:
    return f"resumes/{user_id}/"
