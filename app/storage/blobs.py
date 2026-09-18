"""Blob storage for raw ingest payloads, generated PDFs/cards, and backup
dumps -- never for anything requiring a transaction, uniqueness, or a lock
(design spec Sec11.4). Object keys are always owner-prefixed. Swappable the
same way the L6 composition interface is: LocalFilesystemBlobStore for dev,
GCSBlobStore for deployment."""

import datetime
from pathlib import Path
from typing import Protocol

from google.cloud import storage


def build_blob_key(owner_user_id: str, kind: str, blob_id: str) -> str:
    for part, name in (
        (owner_user_id, "owner_user_id"),
        (kind, "kind"),
        (blob_id, "blob_id"),
    ):
        if "/" in part or "\\" in part or ".." in part:
            raise ValueError(
                f"invalid {name}: {part!r} (no '/', '\\', or '..' allowed)"
            )
    return f"users/{owner_user_id}/{kind}/{blob_id}"


class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def signed_url(self, key: str, expires_in: int = 3600) -> str: ...


class LocalFilesystemBlobStore:
    def __init__(self, root_dir: Path) -> None:
        self._root_dir = Path(root_dir)

    def _path_for(self, key: str) -> Path:
        return self._root_dir / key

    def put(self, key: str, data: bytes) -> None:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path_for(key).read_bytes()

    def signed_url(self, key: str, expires_in: int = 3600) -> str:
        return f"file://{self._path_for(key).as_posix()}"


class GCSBlobStore:
    def __init__(self, bucket_name: str) -> None:
        self._bucket = storage.Client().bucket(bucket_name)

    def put(self, key: str, data: bytes) -> None:
        self._bucket.blob(key).upload_from_string(data)

    def get(self, key: str) -> bytes:
        return self._bucket.blob(key).download_as_bytes()

    def signed_url(self, key: str, expires_in: int = 3600) -> str:
        return self._bucket.blob(key).generate_signed_url(
            expiration=datetime.timedelta(seconds=expires_in)
        )
