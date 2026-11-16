"""Provider-independent storage interface.

Video files, audio and exports are stored under string keys such as
``users/1/videos/42/source.mp4`` (see app.storage.keys). Backends never accept
a key that has not passed validate_key().
"""

import os
import re
import shutil
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO

# Size of each read/write when streaming file contents, so uploads are never
# loaded into memory in full.
CHUNK_SIZE = 1024 * 1024

MAX_KEY_LENGTH = 1024

# A key segment starts with a letter, digit or underscore (so it can never be
# "." or "..", or a hidden/temporary file name) followed by letters, digits,
# ".", "_" or "-".
_SEGMENT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")

# Windows device names (with or without an extension) refer to devices rather
# than files, so they are rejected on every platform to keep keys portable.
_RESERVED_SEGMENT_RE = re.compile(
    r"(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?", re.IGNORECASE
)


class StorageError(Exception):
    """Base class for storage errors."""


class InvalidStorageKeyError(StorageError, ValueError):
    """The key is empty, malformed, or could escape the storage root."""


class StorageObjectNotFoundError(StorageError, FileNotFoundError):
    """No object is stored under the key."""


class StorageObjectTooLargeError(StorageError):
    """The data being saved exceeds the allowed size."""


class StorageConfigurationError(StorageError):
    """The configured storage backend cannot be used."""


def validate_key(key: object) -> str:
    """Return the key unchanged if it is safe to use, else raise.

    A valid key is a relative, "/"-separated path of non-empty segments.
    Keys containing "..", backslashes, absolute paths, drive letters, empty
    segments, characters outside [A-Za-z0-9._-], segments ending in "." and
    Windows device names (CON, NUL, COM1, ...) are rejected.
    """
    if not isinstance(key, str) or not key:
        raise InvalidStorageKeyError("Storage key must be a non-empty string.")
    if len(key) > MAX_KEY_LENGTH:
        raise InvalidStorageKeyError("Storage key is too long.")
    if ".." in key:
        raise InvalidStorageKeyError("Storage key must not contain '..'.")
    if "\\" in key:
        raise InvalidStorageKeyError("Storage key must not contain backslashes.")
    if key.startswith("/"):
        raise InvalidStorageKeyError("Storage key must be a relative path.")
    for segment in key.split("/"):
        if not _SEGMENT_RE.fullmatch(segment):
            raise InvalidStorageKeyError(
                "Storage key contains an invalid path segment."
            )
        # Windows silently strips trailing dots, so "a." would alias "a".
        if segment.endswith("."):
            raise InvalidStorageKeyError("Storage key segments must not end with '.'.")
        if _RESERVED_SEGMENT_RE.fullmatch(segment):
            raise InvalidStorageKeyError("Storage key contains a reserved device name.")
    return key


class StorageBackend(ABC):
    """Interface implemented by every storage backend."""

    @abstractmethod
    def save(self, key: str, fileobj: BinaryIO, *, max_bytes: int | None = None) -> int:
        """Store the contents of fileobj under key and return the bytes written.

        The data is streamed in chunks of at most CHUNK_SIZE bytes. An existing
        object under the key is replaced only once the new data is complete.
        If max_bytes is set and the data is larger, nothing is stored and
        StorageObjectTooLargeError is raised.
        """

    @abstractmethod
    def open(self, key: str) -> BinaryIO:
        """Return a readable binary stream for the object; the caller closes it."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return True if an object is stored under key."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete the object. Deleting a missing object is not an error."""

    @abstractmethod
    def size(self, key: str) -> int:
        """Return the size of the object in bytes."""

    @abstractmethod
    def get_download_url(self, key: str, expires_seconds: int) -> str | None:
        """Return a time-limited download URL, or None.

        None means the backend cannot hand out URLs and the API must stream
        the file itself.
        """

    @contextmanager
    def local_path(self, key: str) -> Iterator[Path]:
        """Yield a local filesystem path holding the object (for FFmpeg).

        This default copies the object to a temporary file, which is removed
        when the context exits. Backends that already keep files locally
        override it. Callers must treat the path as read-only.
        """
        validate_key(key)
        suffix = PurePosixPath(key).suffix
        fd, tmp_name = tempfile.mkstemp(prefix="clipforge-", suffix=suffix)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out, self.open(key) as src:
                shutil.copyfileobj(src, out, CHUNK_SIZE)
            yield tmp_path
        finally:
            tmp_path.unlink(missing_ok=True)
