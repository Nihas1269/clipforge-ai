"""Storage backend for S3-compatible object storage (AWS S3, Cloudflare R2, MinIO)."""

import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import Settings
from app.storage.base import (
    StorageBackend,
    StorageConfigurationError,
    StorageObjectNotFoundError,
    StorageObjectTooLargeError,
    validate_key,
)

logger = logging.getLogger(__name__)

# Network timeouts (seconds) and retry policy for every S3 request, so a slow
# or unreachable provider never blocks a request or job forever.
CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 60
MAX_ATTEMPTS = 3

# SigV4 presigned URLs are valid for at most 7 days.
MAX_PRESIGNED_URL_EXPIRY_SECONDS = 7 * 24 * 60 * 60

# Uploads larger than the threshold use multipart upload. Bounded concurrency
# keeps memory use bounded for non-seekable streams, which are buffered one
# part at a time per worker.
TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=8 * 1024 * 1024,
    multipart_chunksize=8 * 1024 * 1024,
    max_concurrency=4,
)

# Error codes that mean "this object does not exist". HEAD requests have no
# body, so S3 reports a missing object as code "404"; GET reports "NoSuchKey".
# Other 404 codes (such as NoSuchBucket) are configuration errors and are not
# treated as a missing object.
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})

# Settings that must be set when STORAGE_BACKEND=s3.
REQUIRED_SETTINGS = (
    "S3_BUCKET",
    "S3_REGION",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
)


def is_not_found_error(error: ClientError) -> bool:
    """Return True only if the error confirms that the object does not exist."""
    response = error.response or {}
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status == 404 and code in _NOT_FOUND_CODES


class _CountingReader:
    """Read-only wrapper that counts bytes and enforces an optional size limit."""

    def __init__(self, fileobj: BinaryIO, max_bytes: int | None):
        self._fileobj = fileobj
        self._max_bytes = max_bytes
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._fileobj.read(size)
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("fileobj must be a binary stream.")
        self.bytes_read += len(chunk)
        if self._max_bytes is not None and self.bytes_read > self._max_bytes:
            raise StorageObjectTooLargeError(
                f"Object exceeds the maximum size of {self._max_bytes} bytes."
            )
        return bytes(chunk)


def check_s3_settings(settings: Settings) -> None:
    """Raise StorageConfigurationError if the S3 settings are incomplete."""
    missing = [name for name in REQUIRED_SETTINGS if getattr(settings, name) is None]
    if missing:
        raise StorageConfigurationError(
            "STORAGE_BACKEND=s3 requires these settings: "
            f"{', '.join(missing)}. See backend/README.md."
        )
    endpoint = settings.S3_ENDPOINT_URL
    if endpoint is not None and not endpoint.lower().startswith(
        ("http://", "https://")
    ):
        raise StorageConfigurationError(
            "S3_ENDPOINT_URL must start with http:// or https://."
        )


def create_s3_client(settings: Settings) -> Any:
    """Create a boto3 S3 client from validated settings. No request is sent."""
    check_s3_settings(settings)
    config = Config(
        signature_version="s3v4",
        s3={"addressing_style": settings.S3_ADDRESSING_STYLE},
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
        # total_max_attempts counts the first request; botocore's max_attempts
        # counts retries only and would allow one more attempt.
        retries={"total_max_attempts": MAX_ATTEMPTS, "mode": "standard"},
    )
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        region_name=settings.S3_REGION,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY.get_secret_value(),
        config=config,
    )


class S3Storage(StorageBackend):
    """Stores each object in an S3-compatible bucket under its key.

    Keys are validated with the same rules as local storage. Errors other than
    a confirmed missing object (authentication, permission, timeout, server
    errors) are propagated unchanged and are never reported as success.
    """

    def __init__(
        self,
        client: Any,
        bucket: str,
        *,
        transfer_config: TransferConfig = TRANSFER_CONFIG,
    ):
        if not bucket:
            raise StorageConfigurationError("S3 bucket name must not be empty.")
        self.client = client
        self.bucket = bucket
        self.transfer_config = transfer_config

    @classmethod
    def from_settings(cls, settings: Settings) -> "S3Storage":
        return cls(create_s3_client(settings), settings.S3_BUCKET)

    def save(self, key: str, fileobj: BinaryIO, *, max_bytes: int | None = None) -> int:
        validate_key(key)
        reader = _CountingReader(fileobj, max_bytes)
        # S3 replaces an object only once an upload completes; a failed
        # multipart upload is aborted by the transfer manager.
        self.client.upload_fileobj(
            reader, self.bucket, key, Config=self.transfer_config
        )
        logger.info(
            "storage.saved", extra={"storage_key": key, "size_bytes": reader.bytes_read}
        )
        return reader.bytes_read

    def open(self, key: str) -> BinaryIO:
        validate_key(key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if is_not_found_error(exc):
                raise StorageObjectNotFoundError(
                    f"No stored object for key {key!r}."
                ) from None
            raise
        return response["Body"]

    def _head(self, key: str) -> dict[str, Any] | None:
        """Return the object's metadata, or None if it is confirmed missing."""
        validate_key(key)
        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if is_not_found_error(exc):
                return None
            raise

    def exists(self, key: str) -> bool:
        return self._head(key) is not None

    def delete(self, key: str) -> None:
        validate_key(key)
        # S3 reports success when deleting a missing object.
        self.client.delete_object(Bucket=self.bucket, Key=key)
        logger.info("storage.deleted", extra={"storage_key": key})

    def size(self, key: str) -> int:
        head = self._head(key)
        if head is None:
            raise StorageObjectNotFoundError(f"No stored object for key {key!r}.")
        return int(head["ContentLength"])

    def get_download_url(self, key: str, expires_seconds: int) -> str | None:
        validate_key(key)
        if (
            isinstance(expires_seconds, bool)
            or not isinstance(expires_seconds, int)
            or not 0 < expires_seconds <= MAX_PRESIGNED_URL_EXPIRY_SECONDS
        ):
            raise ValueError(
                "expires_seconds must be an integer between 1 and "
                f"{MAX_PRESIGNED_URL_EXPIRY_SECONDS}."
            )
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )

    @contextmanager
    def local_path(self, key: str) -> Iterator[Path]:
        # Download to a temporary file (multipart-capable) that is removed
        # when the context exits, even if the download or the caller fails.
        validate_key(key)
        suffix = PurePosixPath(key).suffix
        fd, tmp_name = tempfile.mkstemp(prefix="clipforge-", suffix=suffix)
        tmp_path = Path(tmp_name)
        try:
            try:
                with os.fdopen(fd, "wb") as out:
                    self.client.download_fileobj(
                        self.bucket, key, out, Config=self.transfer_config
                    )
            except ClientError as exc:
                if is_not_found_error(exc):
                    raise StorageObjectNotFoundError(
                        f"No stored object for key {key!r}."
                    ) from None
                raise
            yield tmp_path
        finally:
            tmp_path.unlink(missing_ok=True)
