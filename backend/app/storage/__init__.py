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
from app.storage.s3 import S3Storage

__all__ = [
    "CHUNK_SIZE",
    "InvalidStorageKeyError",
    "LocalStorage",
    "S3Storage",
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
    """Return the storage backend selected by STORAGE_BACKEND.

    Raises StorageConfigurationError if the selected backend is not fully
    configured (for example a missing S3 setting).
    """
    if settings is None:
        settings = get_settings()
    if settings.STORAGE_BACKEND == "local":
        return LocalStorage(settings.LOCAL_STORAGE_DIR)
    if settings.STORAGE_BACKEND == "s3":
        return S3Storage.from_settings(settings)
    raise StorageConfigurationError(
        f"STORAGE_BACKEND={settings.STORAGE_BACKEND!r} is not supported; "
        "use 'local' or 's3'."
    )
