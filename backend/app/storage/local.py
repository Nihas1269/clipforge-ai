"""Storage backend that keeps objects on the local filesystem."""

import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from app.storage.base import (
    CHUNK_SIZE,
    InvalidStorageKeyError,
    StorageBackend,
    StorageObjectNotFoundError,
    StorageObjectTooLargeError,
    validate_key,
)

logger = logging.getLogger(__name__)


class LocalStorage(StorageBackend):
    """Stores each object as a file under a root directory.

    Every path is resolved (following symlinks) and checked to lie beneath the
    root before it is read, written or deleted, so neither crafted keys nor
    symlinks inside the root can reach files outside it.
    """

    def __init__(self, root: str | os.PathLike[str], *, chunk_size: int = CHUNK_SIZE):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive.")
        self.root = Path(root).resolve()
        self.chunk_size = chunk_size

    def _resolve(self, key: str) -> Path:
        validate_key(key)
        path = (self.root / key).resolve()
        if path == self.root or not path.is_relative_to(self.root):
            raise InvalidStorageKeyError(
                "Storage key resolves outside the storage root."
            )
        return path

    def _existing_file(self, key: str) -> Path:
        path = self._resolve(key)
        if not path.is_file():
            raise StorageObjectNotFoundError(f"No stored object for key {key!r}.")
        return path

    def save(self, key: str, fileobj: BinaryIO, *, max_bytes: int | None = None) -> int:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Check again now that the directories exist, in case one of them is
        # a symlink pointing outside the root.
        path = self._resolve(key)

        # Write to a temporary file in the same directory and move it into
        # place only when complete, so a failed upload never leaves a partial
        # object behind. The "." prefix can never match a valid key.
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=".upload-", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        written = 0
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = fileobj.read(self.chunk_size)
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise TypeError("fileobj must be a binary stream.")
                    if not chunk:
                        break
                    written += len(chunk)
                    if max_bytes is not None and written > max_bytes:
                        raise StorageObjectTooLargeError(
                            f"Object exceeds the maximum size of {max_bytes} bytes."
                        )
                    out.write(chunk)
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        logger.info("storage.saved", extra={"storage_key": key, "size_bytes": written})
        return written

    def open(self, key: str) -> BinaryIO:
        return self._existing_file(key).open("rb")

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def delete(self, key: str) -> None:
        path = self._resolve(key)
        if path.is_dir():
            raise InvalidStorageKeyError(
                "Storage key refers to a directory, not an object."
            )
        try:
            path.unlink()
        except FileNotFoundError:
            return
        logger.info("storage.deleted", extra={"storage_key": key})

    def size(self, key: str) -> int:
        return self._existing_file(key).stat().st_size

    def get_download_url(self, key: str, expires_seconds: int) -> str | None:
        validate_key(key)
        # Local files are never served directly; the API streams them.
        return None

    @contextmanager
    def local_path(self, key: str) -> Iterator[Path]:
        # The file is already local, so no copy is needed.
        yield self._existing_file(key)
