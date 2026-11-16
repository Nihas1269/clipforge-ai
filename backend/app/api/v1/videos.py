"""Video upload.

`POST /videos` accepts a multipart form with a single file part named `file`.
Authentication is checked before any of the request body is read. The body is
then parsed as it streams in: the file part is spooled to a temporary file
(at most 1 MiB is held in memory) and reading stops as soon as the body
exceeds MAX_UPLOAD_SIZE_MB plus a small allowance for the multipart framing.
Once the extension, declared content type and file signature have been
validated, the file is streamed to storage under a generated key and a
`Video` row is created with status `uploaded`. Processing is not started here.

Validation checks the file format only; it is not malware scanning. The
client's file name is sanitized and stored for display only; it is never used
in a storage key, a path or a command.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from app.api.deps import get_current_user
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.database import get_db
from app.models.user import User
from app.models.video import Video, VideoStatus
from app.schemas.video import UploadLimits, VideoRead
from app.services.video_upload import (
    CANONICAL_CONTENT_TYPES,
    CONTENT_TYPE_CONTAINERS,
    EXTENSION_CONTAINERS,
    SNIFF_BYTES,
    detect_container,
    file_extension,
    normalize_content_type,
    sanitize_filename,
)
from app.storage import (
    InvalidStorageKeyError,
    StorageBackend,
    StorageConfigurationError,
    StorageObjectTooLargeError,
    get_storage,
    video_source_key,
)

logger = logging.getLogger(__name__)

BYTES_PER_MB = 1024 * 1024
# Allowance on top of MAX_UPLOAD_SIZE_MB for multipart boundaries and part
# headers. The file itself is checked against the exact limit.
MULTIPART_OVERHEAD_BYTES = 64 * 1024
UPLOAD_FIELD_NAME = "file"

_UPLOAD_OPENAPI: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": [UPLOAD_FIELD_NAME],
                    "properties": {
                        UPLOAD_FIELD_NAME: {"type": "string", "format": "binary"}
                    },
                }
            }
        },
    }
}

router = APIRouter(prefix="/videos", tags=["videos"])


class _RequestBodyTooLargeError(MultiPartException):
    """The request body grew past the upload limit while streaming."""


class _UploadInterruptedError(MultiPartException):
    """The client disconnected before the request body was complete.

    Raised as a MultiPartException so the parser closes (and so deletes) the
    temporary files it has spooled so far.
    """


def get_video_storage(settings: Settings = Depends(get_settings)) -> StorageBackend:
    """Return the configured storage backend, or 503 if it is misconfigured."""
    try:
        return get_storage(settings)
    except StorageConfigurationError:
        logger.error("video_upload_storage_unavailable")
        raise AppError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "storage_unavailable",
            "File storage is not available.",
        ) from None


def _loggable(value: str | None, known: dict[str, str]) -> str | None:
    """Return value if it is a recognized type, else "other".

    Client-supplied extensions and content types are not logged verbatim, so
    that parts of a private file name cannot end up in the logs.
    """
    if not value:
        return None
    return value if value in known else "other"


def _reject(
    user: User,
    status_code: int,
    code: str,
    message: str,
    *,
    size_bytes: int | None = None,
    extension: str | None = None,
    content_type: str | None = None,
) -> AppError:
    """Log a rejected upload and return the error to raise."""
    logger.info(
        "video_upload_rejected",
        extra={
            "user_id": str(user.id),
            "reason": code,
            "size_bytes": size_bytes,
            "extension": _loggable(extension, EXTENSION_CONTAINERS),
            "content_type": _loggable(content_type, CONTENT_TYPE_CONTAINERS),
        },
    )
    return AppError(status_code, code, message)


def _too_large(
    user: User,
    max_bytes: int,
    size_bytes: int | None = None,
    *,
    extension: str | None = None,
    content_type: str | None = None,
) -> AppError:
    return _reject(
        user,
        413,
        "file_too_large",
        f"The file exceeds the maximum upload size of {max_bytes // BYTES_PER_MB} MB.",
        size_bytes=size_bytes,
        extension=extension,
        content_type=content_type,
    )


def _unsupported(
    user: User,
    code: str,
    message: str,
    *,
    extension: str | None = None,
    content_type: str | None = None,
) -> AppError:
    return _reject(
        user,
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        code,
        message,
        extension=extension,
        content_type=content_type,
    )


def _delete_quietly(storage: StorageBackend, key: str) -> None:
    """Best-effort removal of a stored object after a failed upload."""
    try:
        storage.delete(key)
    except Exception:
        logger.exception("video_upload_cleanup_failed")


async def _limited_body(request: Request, limit: int) -> AsyncIterator[bytes]:
    """Yield the request body, raising once more than `limit` bytes arrive."""
    received = 0
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            raise _UploadInterruptedError("The client disconnected.")
        chunk = message.get("body", b"")
        received += len(chunk)
        if received > limit:
            raise _RequestBodyTooLargeError("Request body is too large.")
        if chunk:
            yield chunk
        if not message.get("more_body", False):
            return


async def _parse_upload_form(request: Request, user: User, max_bytes: int) -> FormData:
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            too_long = int(declared_length) > max_bytes + MULTIPART_OVERHEAD_BYTES
        except ValueError:
            too_long = False
        if too_long:
            # Rejected before any of the body is read.
            raise _too_large(user, max_bytes)

    request_content_type = request.headers.get("content-type")
    media_type = normalize_content_type(request_content_type)
    if media_type != "multipart/form-data":
        raise _unsupported(
            user, "invalid_request_content_type", "The request must be multipart/form-data."
        )
    # Older Starlette versions raise KeyError (a 500) when the boundary is
    # missing, so reject it here.
    params = (request_content_type or "").split(";")[1:]
    if not any(p.strip().lower().startswith("boundary=") for p in params):
        raise _reject(
            user,
            status.HTTP_400_BAD_REQUEST,
            "invalid_multipart",
            "The multipart request is missing its boundary.",
        )

    parser = MultiPartParser(
        request.headers,
        _limited_body(request, max_bytes + MULTIPART_OVERHEAD_BYTES),
        max_files=1,
        max_fields=0,
    )
    try:
        return await parser.parse()
    except _RequestBodyTooLargeError:
        raise _too_large(user, max_bytes) from None
    except (_UploadInterruptedError, ClientDisconnect):
        # Nothing has reached storage yet. Close the temporary files spooled
        # so far now rather than waiting for garbage collection; recent
        # Starlette versions already do this, and closing twice is harmless.
        for spooled in getattr(parser, "_files_to_close_on_error", ()):
            spooled.close()
        logger.info("video_upload_interrupted", extra={"user_id": str(user.id)})
        raise AppError(
            status.HTTP_400_BAD_REQUEST, "upload_interrupted", "The upload was interrupted."
        ) from None
    except MultiPartException:
        raise _reject(
            user,
            status.HTTP_400_BAD_REQUEST,
            "invalid_multipart",
            f"The request must contain exactly one file part named '{UPLOAD_FIELD_NAME}'.",
        ) from None


def _store_upload(
    upload: UploadFile,
    user: User,
    db: Session,
    storage: StorageBackend,
    settings: Settings,
    max_bytes: int,
) -> Video:
    """Validate the spooled upload, stream it to storage and create the row.

    Runs in a worker thread because file, storage and database calls block.
    """
    filename = sanitize_filename(upload.filename)
    ext = file_extension(filename)
    declared_type = normalize_content_type(upload.content_type)
    types = {"extension": ext, "content_type": declared_type}

    if ext not in EXTENSION_CONTAINERS or ext not in settings.ALLOWED_VIDEO_EXTENSIONS:
        raise _unsupported(
            user, "unsupported_file_type", "This file type is not supported.", **types
        )
    expected_container = EXTENSION_CONTAINERS[ext]

    if declared_type not in settings.ALLOWED_VIDEO_CONTENT_TYPES:
        raise _unsupported(
            user, "unsupported_content_type", "This content type is not supported.", **types
        )
    if CONTENT_TYPE_CONTAINERS.get(declared_type) != expected_container:
        raise _unsupported(
            user,
            "file_type_mismatch",
            "The declared content type does not match the file extension.",
            **types,
        )

    if upload.size is not None and upload.size > max_bytes:
        raise _too_large(user, max_bytes, upload.size, **types)

    fileobj = upload.file
    fileobj.seek(0)
    header = fileobj.read(SNIFF_BYTES)
    if not header:
        raise _reject(user, 422, "empty_file", "The file is empty.", size_bytes=0, **types)
    if detect_container(header) != expected_container:
        raise _unsupported(
            user,
            "file_type_mismatch",
            "The file contents do not match a supported video format for its extension.",
            **types,
        )
    fileobj.seek(0)

    video_id = uuid.uuid4()
    # The key is generated from ids only; the client's file name is never used.
    try:
        key = video_source_key(str(user.id), str(video_id), ext)
    except InvalidStorageKeyError:
        # Nothing has been stored yet, so there is nothing to clean up.
        logger.exception("video_upload_storage_key_invalid", extra={"user_id": str(user.id)})
        raise AppError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "upload_failed",
            "The upload could not be stored.",
        ) from None
    content_type = CANONICAL_CONTENT_TYPES[ext]

    try:
        size_bytes = storage.save(key, fileobj, max_bytes=max_bytes)
    except StorageObjectTooLargeError:
        _delete_quietly(storage, key)
        raise _too_large(user, max_bytes, **types) from None
    except Exception:
        logger.exception("video_upload_storage_failed", extra={"user_id": str(user.id)})
        _delete_quietly(storage, key)
        raise AppError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "upload_failed",
            "The upload could not be stored.",
        ) from None

    video = Video(
        id=video_id,
        user_id=user.id,
        original_filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        storage_key=key,
        status=VideoStatus.UPLOADED,
    )
    try:
        db.add(video)
        db.commit()
    except Exception as exc:
        db.rollback()
        _delete_quietly(storage, key)
        # Only the exception type is logged: SQLAlchemy error messages include
        # the bound parameters, and so the client's (private) file name.
        logger.error(
            "video_upload_db_failed",
            extra={"user_id": str(user.id), "error_type": type(exc).__name__},
        )
        raise AppError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "upload_failed",
            "The upload could not be saved.",
        ) from None

    logger.info(
        "video_uploaded",
        extra={
            "user_id": str(user.id),
            "video_id": str(video.id),
            "size_bytes": size_bytes,
            "content_type": content_type,
        },
    )
    return video


@router.get("/upload-limits", response_model=UploadLimits)
def get_upload_limits(
    _user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> UploadLimits:
    """Return the limits `POST /videos` enforces, so clients can check files first.

    Only allowlist entries the upload endpoint actually accepts (those with a
    known file signature) are returned.
    """
    return UploadLimits(
        max_upload_size_bytes=settings.MAX_UPLOAD_SIZE_MB * BYTES_PER_MB,
        allowed_extensions=[
            ext for ext in settings.ALLOWED_VIDEO_EXTENSIONS if ext in EXTENSION_CONTAINERS
        ],
        allowed_content_types=[
            content_type
            for content_type in settings.ALLOWED_VIDEO_CONTENT_TYPES
            if content_type in CONTENT_TYPE_CONTAINERS
        ],
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=VideoRead,
    openapi_extra=_UPLOAD_OPENAPI,
)
async def upload_video(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    storage: StorageBackend = Depends(get_video_storage),
) -> VideoRead:
    """Upload a video file (multipart field `file`) and return the new video."""
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * BYTES_PER_MB
    form = await _parse_upload_form(request, user, max_bytes)
    try:
        upload = form.get(UPLOAD_FIELD_NAME)
        if not isinstance(upload, UploadFile):
            raise _reject(
                user,
                422,
                "missing_file",
                f"A file part named '{UPLOAD_FIELD_NAME}' is required.",
            )
        video = await run_in_threadpool(
            _store_upload, upload, user, db, storage, settings, max_bytes
        )
    finally:
        await form.close()
    return VideoRead.model_validate(video)
