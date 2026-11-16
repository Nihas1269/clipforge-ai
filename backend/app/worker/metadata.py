"""Metadata extraction after upload (job type `metadata`).

The upload endpoint calls queue_metadata_extraction() once the video row is
committed. The extract_metadata task then:

    uploaded | failed | ready -> processing -> ready   (metadata saved)
                                            -> failed  (with a message)

It probes the stored file through the storage abstraction (a local copy for
remote backends) with ffprobe and saves the duration, dimensions and frame
rate. Files that are not valid videos, have no video stream, or are longer
than MAX_VIDEO_DURATION_SECONDS end in `failed` with a message for the owner.

Every failure moves both the video (`failed`) and the job (`failed`) to a
terminal state; only a temporary storage read error is retried, and the
video stays `processing` only while a retry is queued. Running the task
again for the same video re-probes the file and overwrites the metadata, so
retries and reprocessing never duplicate anything.
"""

import logging
import tempfile
import uuid
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import database
from app.core.config import Settings, get_settings
from app.media.ffmpeg import (
    FFmpegNotFoundError,
    MediaError,
    MediaInfo,
    MediaTimeoutError,
    probe,
)
from app.models.processing_job import JobType, ProcessingJob
from app.models.video import Video, VideoStatus, transition_status
from app.storage import (
    InvalidStorageKeyError,
    LocalStorage,
    StorageBackend,
    StorageConfigurationError,
    StorageError,
    StorageObjectNotFoundError,
    get_storage,
)
from app.worker.jobs import (
    JobContext,
    JobError,
    RetryableJobError,
    enqueue_job,
    job_task,
)

logger = logging.getLogger(__name__)

EXTRACT_METADATA_TASK_NAME = "app.worker.metadata.extract_metadata"
METADATA_MAX_ATTEMPTS = 3
# Covers downloading a large file from remote storage plus the probe
# (FFPROBE_TIMEOUT_SECONDS, at most 600 s).
METADATA_SOFT_TIME_LIMIT_SECONDS = 900
METADATA_TIME_LIMIT_SECONDS = 960

# Messages stored on the video and the job. They are shown to the video's
# owner, so they never contain paths, keys or tool output.
INVALID_PAYLOAD_MESSAGE = "The metadata job is invalid."
VIDEO_MISSING_MESSAGE = "The video no longer exists."
QUEUE_FAILURE_MESSAGE = "Processing could not be started. Try again later."
STORAGE_UNAVAILABLE_MESSAGE = "File storage is not available."
FILE_MISSING_MESSAGE = "The uploaded file could not be found."
FILE_UNREADABLE_MESSAGE = "The uploaded file could not be read."
INVALID_LOCATION_MESSAGE = "The uploaded file is not stored where expected."
FFMPEG_UNAVAILABLE_MESSAGE = (
    "Video processing is not available because FFmpeg is not installed or "
    "configured on the server."
)
PROBE_TIMEOUT_MESSAGE = "Reading the video took too long."
INVALID_VIDEO_MESSAGE = "The file is not a valid video."
NO_VIDEO_STREAM_MESSAGE = "The file has no video stream."
UNKNOWN_DURATION_MESSAGE = "The video's duration could not be determined."
UNKNOWN_DIMENSIONS_MESSAGE = "The video's dimensions could not be determined."
TIMED_OUT_MESSAGE = "Processing the video timed out."
UNEXPECTED_FAILURE_MESSAGE = "Video processing failed unexpectedly."


def _too_long_message(duration: float, limit: int) -> str:
    return (
        f"The video is too long ({duration:.0f} seconds); the maximum is "
        f"{limit} seconds."
    )


def _video_log(video_id: uuid.UUID, **extra: object) -> dict[str, object]:
    return {"video_id": str(video_id), **extra}


# Probing


def _expected_key_prefix(video: Video) -> str:
    return f"users/{video.user_id}/videos/{video.id}/"


def _permitted_roots(storage: StorageBackend) -> list[Path]:
    if isinstance(storage, LocalStorage):
        return [storage.root]
    # Remote backends copy the object to a temporary file.
    return [Path(tempfile.gettempdir()).resolve()]


def _check_permitted(path: Path, storage: StorageBackend) -> Path:
    """Return the resolved path if it is a file inside the permitted area."""
    resolved = Path(path).resolve()
    if not resolved.is_file() or not any(
        resolved.is_relative_to(root) for root in _permitted_roots(storage)
    ):
        raise JobError(INVALID_LOCATION_MESSAGE)
    return resolved


def _probe_stored_video(
    video: Video, storage: StorageBackend | None, settings: Settings
) -> MediaInfo:
    """Probe the video's stored file; raise JobError with a public message."""
    key = video.storage_key
    # The key is generated from the ids at upload; anything else is refused.
    if not isinstance(key, str) or not key.startswith(_expected_key_prefix(video)):
        logger.error("video_metadata_unexpected_key", extra=_video_log(video.id))
        raise JobError(INVALID_LOCATION_MESSAGE)
    if storage is None:
        try:
            storage = get_storage(settings)
        except StorageConfigurationError:
            logger.error(
                "video_metadata_storage_unavailable", extra=_video_log(video.id)
            )
            raise JobError(STORAGE_UNAVAILABLE_MESSAGE) from None

    try:
        with storage.local_path(key) as path:
            source = _check_permitted(path, storage)
            return probe(
                source, timeout=settings.FFPROBE_TIMEOUT_SECONDS, settings=settings
            )
    except JobError:
        raise
    except FFmpegNotFoundError:
        logger.error("video_metadata_ffprobe_missing", extra=_video_log(video.id))
        raise JobError(FFMPEG_UNAVAILABLE_MESSAGE) from None
    except MediaTimeoutError:
        logger.warning("video_metadata_probe_timeout", extra=_video_log(video.id))
        raise JobError(PROBE_TIMEOUT_MESSAGE) from None
    except MediaError:
        # The tool output is not logged: it can echo parts of the file.
        logger.info("video_metadata_invalid_file", extra=_video_log(video.id))
        raise JobError(INVALID_VIDEO_MESSAGE) from None
    except StorageObjectNotFoundError:
        logger.warning("video_metadata_file_missing", extra=_video_log(video.id))
        raise JobError(FILE_MISSING_MESSAGE) from None
    except InvalidStorageKeyError:
        logger.error("video_metadata_invalid_key", extra=_video_log(video.id))
        raise JobError(INVALID_LOCATION_MESSAGE) from None
    except (OSError, StorageError, BotoCoreError, ClientError) as exc:
        # Network or disk errors while reading the file may be temporary.
        # Anything else is a bug and propagates as an unexpected failure.
        logger.warning(
            "video_metadata_read_failed",
            extra=_video_log(video.id, error_type=type(exc).__name__),
        )
        raise RetryableJobError(FILE_UNREADABLE_MESSAGE) from None


def _check_metadata(info: MediaInfo, settings: Settings) -> None:
    """Raise JobError if the probed file is not an acceptable video."""
    if not info.has_video:
        raise JobError(NO_VIDEO_STREAM_MESSAGE)
    if info.duration_seconds is None:
        raise JobError(UNKNOWN_DURATION_MESSAGE)
    if info.width is None or info.height is None:
        raise JobError(UNKNOWN_DIMENSIONS_MESSAGE)
    limit = settings.MAX_VIDEO_DURATION_SECONDS
    if info.duration_seconds > limit:
        raise JobError(_too_long_message(info.duration_seconds, limit))


# Video state


def _load_for_update(db: Session, video_id: uuid.UUID) -> Video | None:
    # Locks the row on PostgreSQL so concurrent status changes are serialized,
    # and reloads it so changes made by others are seen.
    return db.scalars(
        select(Video)
        .where(Video.id == video_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()


def _mark_failed(db: Session, video_id: uuid.UUID, message: str) -> None:
    """Move the video from processing to failed; never raises."""
    try:
        db.rollback()
        db.expire_all()
        video = _load_for_update(db, video_id)
        if video is None or video.status is not VideoStatus.PROCESSING:
            db.rollback()
            return
        transition_status(video, VideoStatus.FAILED, error=message)
        # A failed video has no trusted metadata.
        video.duration_seconds = None
        video.width = None
        video.height = None
        video.fps = None
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error(
            "video_metadata_fail_update_failed",
            extra=_video_log(video_id, error_type=type(exc).__name__),
        )


def extract_video_metadata(
    db: Session,
    video_id: uuid.UUID,
    *,
    settings: Settings | None = None,
    storage: StorageBackend | None = None,
    final_attempt: bool = True,
) -> Video:
    """Probe a video, save its metadata and mark it ready; return the video.

    On failure the video is marked failed with a public message and JobError
    is raised with the same message. A RetryableJobError (temporary storage
    error) leaves the video processing when `final_attempt` is False, because
    the job will be retried; on the final attempt the video fails too.
    """
    settings = settings or get_settings()
    video = _load_for_update(db, video_id)
    if video is None:
        db.rollback()
        raise JobError(VIDEO_MISSING_MESSAGE)
    if video.status is not VideoStatus.PROCESSING:
        transition_status(video, VideoStatus.PROCESSING)
    db.commit()
    logger.info("video_metadata_started", extra=_video_log(video_id))

    try:
        info = _probe_stored_video(video, storage, settings)
        _check_metadata(info, settings)
        video = _load_for_update(db, video_id)
        if video is None:
            raise JobError(VIDEO_MISSING_MESSAGE)
        video.duration_seconds = info.duration_seconds
        video.width = info.width
        video.height = info.height
        video.fps = info.fps
        transition_status(video, VideoStatus.READY)
        db.commit()
    except RetryableJobError as exc:
        if not final_attempt:
            db.rollback()
            raise
        _mark_failed(db, video_id, exc.public_message)
        raise JobError(exc.public_message) from None
    except JobError as exc:
        _mark_failed(db, video_id, exc.public_message)
        logger.info(
            "video_metadata_rejected",
            extra=_video_log(video_id, reason=exc.public_message),
        )
        raise
    except SoftTimeLimitExceeded:
        _mark_failed(db, video_id, TIMED_OUT_MESSAGE)
        raise
    except Exception as exc:
        _mark_failed(db, video_id, UNEXPECTED_FAILURE_MESSAGE)
        logger.error(
            "video_metadata_failed",
            extra=_video_log(video_id, error_type=type(exc).__name__),
        )
        raise

    logger.info(
        "video_metadata_extracted",
        extra=_video_log(
            video_id,
            duration_seconds=info.duration_seconds,
            width=info.width,
            height=info.height,
            fps=info.fps,
            has_audio=info.has_audio,
            video_codec=info.video_codec,
            audio_codec=info.audio_codec,
        ),
    )
    return video


# Task and queuing


@job_task(
    JobType.METADATA,
    name=EXTRACT_METADATA_TASK_NAME,
    max_attempts=METADATA_MAX_ATTEMPTS,
    soft_time_limit=METADATA_SOFT_TIME_LIMIT_SECONDS,
    time_limit=METADATA_TIME_LIMIT_SECONDS,
)
def extract_metadata(ctx: JobContext) -> None:
    """Extract the metadata of the video in `payload["video_id"]`."""
    try:
        video_id = uuid.UUID(str(ctx.payload.get("video_id")))
    except ValueError:
        raise JobError(INVALID_PAYLOAD_MESSAGE) from None
    if ctx.video_id is not None and ctx.video_id != video_id:
        raise JobError(INVALID_PAYLOAD_MESSAGE)
    with database.get_session_factory()() as db:
        extract_video_metadata(
            db, video_id, final_attempt=ctx.attempt >= ctx.max_attempts
        )


def queue_metadata_extraction(
    db: Session, video: Video, *, request_id: str | None = None
) -> ProcessingJob | None:
    """Queue metadata extraction for a newly uploaded video.

    Returns the job, or None if it could not be queued. In that case the job
    (if it was written) is failed by enqueue_job and the video is moved to
    `failed` with QUEUE_FAILURE_MESSAGE, so nothing is left waiting forever.
    """
    video_id = video.id
    try:
        return enqueue_job(
            JobType.METADATA,
            extract_metadata,
            db=db,
            user_id=video.user_id,
            video_id=video_id,
            payload={"video_id": str(video_id)},
            idempotency_key=str(video_id),
            request_id=request_id,
        )
    except Exception as exc:
        logger.error(
            "video_metadata_queue_failed",
            extra=_video_log(video_id, error_type=type(exc).__name__),
        )
    try:
        db.rollback()
        db.expire_all()
        current = _load_for_update(db, video_id)
        if current is not None and current.status is VideoStatus.UPLOADED:
            transition_status(current, VideoStatus.PROCESSING)
            transition_status(current, VideoStatus.FAILED, error=QUEUE_FAILURE_MESSAGE)
            db.commit()
        else:
            db.rollback()
    except Exception as exc:
        db.rollback()
        logger.error(
            "video_metadata_queue_fail_update_failed",
            extra=_video_log(video_id, error_type=type(exc).__name__),
        )
    return None


__all__ = [
    "EXTRACT_METADATA_TASK_NAME",
    "extract_metadata",
    "extract_video_metadata",
    "queue_metadata_extraction",
]
