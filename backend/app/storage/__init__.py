"""File storage for videos, audio and exports.

Use get_storage() to obtain the backend selected by Settings.STORAGE_BACKEND.
"""

from app.core.config import Settings, get_settings
from app.storage.base import (
    CHUNK_SIZE,
    InvalidStorageKeyError,
    StorageBackend,
    StorageConfigurationError,
    StorageError,
    StorageObjectNotFoundError,
    StorageObjectTooLargeError,
    validate_key,
)
from app.storage.keys import video_source_key
from app.storage.local import LocalStorage

__all__ = [
    "CHUNK_SIZE",
    "InvalidStorageKeyError",
    "LocalStorage",
    "StorageBackend",
    "StorageConfigurationError",
    "StorageError",
    "StorageObjectNotFoundError",
    "StorageObjectTooLargeError",
    "get_storage",
    "validate_key",
    "video_source_key",
]


def get_storage(settings: Settings | None = None) -> StorageBackend:
    """Return the storage backend selected by STORAGE_BACKEND."""
    if settings is None:
        settings = get_settings()
    if settings.STORAGE_BACKEND == "local":
        return LocalStorage(settings.LOCAL_STORAGE_DIR)
    raise StorageConfigurationError(
        f"STORAGE_BACKEND={settings.STORAGE_BACKEND!r} is not implemented yet; "
        "use 'local'."
    )
