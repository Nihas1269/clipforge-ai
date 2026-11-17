"""Transcription of a ready video (job type `transcription`).

`POST /videos/{id}/transcription` calls queue_transcription(), which queues
the transcribe_video task with the payload `{"video_id": ..., "language":
...}` (language None for automatic detection). The task runs
run_transcription(), which:

1. checks that the video exists and is `ready`;
2. creates a temporary directory, copies the stored file to a local path
   through the storage abstraction and extracts its audio there as 16 kHz
   mono WAV (app.media.ffmpeg.extract_audio);
3. sends the audio to the configured provider (get_transcription_provider);
4. checks the result against the video's duration and stores it, replacing
   any earlier transcript and its segments in a single transaction.

The temporary directory (and the remote-storage copy) is always removed,
whether the job succeeds, fails or times out. The video's own status is
never changed by transcription; the job alone records success or failure.

Until step 4 commits, the previous transcript stays exactly as it was: a
failure at any point (including while saving, which is rolled back) never
leaves a partial or empty transcript behind. A cancelled job does not save
its result.

Retries: temporary failures (storage read errors and provider errors other
than an invalid result or a configuration problem) raise RetryableJobError
and are retried with backoff, up to TRANSCRIPTION_MAX_ATTEMPTS attempts.
Every attempt starts from scratch, and saving replaces the transcript, so a
retry never duplicates segments. Everything else fails the job at once with
a message that is safe to show to the owner.

All timestamps are in source-video seconds. Results are checked with
TranscriptionResult.check_within_duration() against the video's duration
plus DURATION_TOLERANCE_SECONDS (audio streams can be slightly longer than
the container reports); times inside that tolerance are clamped to the
duration, anything later fails the job as an invalid result.
"""

import logging
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import database
from app.core.config import Settings, get_settings
from app.media.ffmpeg import (
    DEFAULT_SAMPLE_RATE,
    FFmpegNotFoundError,
    MediaError,
    MediaTimeoutError,
    extract_audio,
)
from app.models.processing_job import ACTIVE_JOB_STATUSES, JobType, ProcessingJob
from app.models.transcript import MODEL_MAX_LENGTH, PROVIDER_MAX_LENGTH, Transcript
from app.models.video import Video, VideoStatus
from app.repositories.transcripts import replace_transcript
from app.storage import (
    InvalidStorageKeyError,
    StorageBackend,
    StorageConfigurationError,
    StorageError,
    StorageObjectNotFoundError,
    get_storage,
)
from app.transcription import (
    InvalidLanguageError,
    InvalidTranscriptionResultError,
    TranscriptionConfigurationError,
    TranscriptionError,
    TranscriptionProvider,
    TranscriptionResult,
    TranscriptionSegment,
    WordTiming,
    get_transcription_provider,
    normalize_language,
)
from app.worker.jobs import (
    JobContext,
    JobError,
    RetryableJobError,
    enqueue_job,
    job_task,
)

logger = logging.getLogger(__name__)

TRANSCRIBE_VIDEO_TASK_NAME = "app.worker.transcription.transcribe_video"
TRANSCRIPTION_MAX_ATTEMPTS = 3
# Covers copying the file from remote storage, extracting the audio
# (FFMPEG_TIMEOUT_SECONDS, at most 3600 s but 1800 s by default) and the
# provider call; the hard limit may not exceed jobs.MAX_TIME_LIMIT_SECONDS.
TRANSCRIPTION_SOFT_TIME_LIMIT_SECONDS = 3300
TRANSCRIPTION_TIME_LIMIT_SECONDS = 3420
AUDIO_SAMPLE_RATE = DEFAULT_SAMPLE_RATE
AUDIO_FILENAME = "audio.wav"
TEMP_DIR_PREFIX = "clipforge-transcription-"
DURATION_TOLERANCE_SECONDS = 1.0

# Messages stored on the job. They are shown to the video's owner, so they
# never contain paths, keys, tool output or transcript text.
INVALID_PAYLOAD_MESSAGE = "The transcription job is invalid."
VIDEO_MISSING_MESSAGE = "The video no longer exists."
VIDEO_NOT_READY_MESSAGE = "The video is not ready for transcription."
VIDEO_CHANGED_MESSAGE = "The video changed while it was being transcribed. Try again."
PROVIDER_UNAVAILABLE_MESSAGE = "Transcription is not available on the server."
STORAGE_UNAVAILABLE_MESSAGE = "File storage is not available."
FILE_MISSING_MESSAGE = "The uploaded file could not be found."
FILE_UNREADABLE_MESSAGE = "The uploaded file could not be read."
INVALID_LOCATION_MESSAGE = "The uploaded file is not stored where expected."
FFMPEG_UNAVAILABLE_MESSAGE = (
    "Audio extraction is not available because FFmpeg is not installed or "
    "configured on the server."
)
AUDIO_TIMEOUT_MESSAGE = "Extracting the audio took too long."
NO_AUDIO_MESSAGE = (
    "The video's audio could not be extracted. It may have no audio track."
)
PROVIDER_FAILED_MESSAGE = "The transcription service failed. Try again later."
INVALID_RESULT_MESSAGE = "The transcription service returned an invalid result."


def _video_log(video_id: uuid.UUID, **extra: object) -> dict[str, object]:
    return {"video_id": str(video_id), **extra}


def _load_video(
    db: Session, video_id: uuid.UUID, *, lock: bool = False
) -> Video | None:
    statement = (
        select(Video)
        .where(Video.id == video_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        # Serializes the save with status changes and deletion on PostgreSQL.
        statement = statement.with_for_update()
    return db.scalars(statement).one_or_none()


# Audio


def _extract_audio(
    video_id: uuid.UUID,
    user_id: uuid.UUID,
    key: str,
    storage: StorageBackend | None,
    settings: Settings,
    workdir: Path,
) -> Path:
    """Extract the stored video's audio into `workdir`; raise JobError on failure."""
    # The key is generated from the ids at upload; anything else is refused.
    if not isinstance(key, str) or not key.startswith(
        f"users/{user_id}/videos/{video_id}/"
    ):
        logger.error("transcription_unexpected_key", extra=_video_log(video_id))
        raise JobError(INVALID_LOCATION_MESSAGE)
    if storage is None:
        try:
            storage = get_storage(settings)
        except StorageConfigurationError:
            logger.error(
                "transcription_storage_unavailable", extra=_video_log(video_id)
            )
            raise JobError(STORAGE_UNAVAILABLE_MESSAGE) from None

    target = workdir / AUDIO_FILENAME
    try:
        with storage.local_path(key) as source:
            return extract_audio(source, target, AUDIO_SAMPLE_RATE, settings=settings)
    except FFmpegNotFoundError:
        logger.error("transcription_ffmpeg_missing", extra=_video_log(video_id))
        raise JobError(FFMPEG_UNAVAILABLE_MESSAGE) from None
    except MediaTimeoutError:
        logger.warning("transcription_audio_timeout", extra=_video_log(video_id))
        raise JobError(AUDIO_TIMEOUT_MESSAGE) from None
    except MediaError:
        # The tool output is not logged: it can echo parts of the file.
        logger.info("transcription_audio_failed", extra=_video_log(video_id))
        raise JobError(NO_AUDIO_MESSAGE) from None
    except StorageObjectNotFoundError:
        logger.warning("transcription_file_missing", extra=_video_log(video_id))
        raise JobError(FILE_MISSING_MESSAGE) from None
    except InvalidStorageKeyError:
        logger.error("transcription_invalid_key", extra=_video_log(video_id))
        raise JobError(INVALID_LOCATION_MESSAGE) from None
    except (OSError, StorageError, BotoCoreError, ClientError) as exc:
        # Network or disk errors while reading the file may be temporary.
        logger.warning(
            "transcription_read_failed",
            extra=_video_log(video_id, error_type=type(exc).__name__),
        )
        raise RetryableJobError(FILE_UNREADABLE_MESSAGE) from None


# Provider


def _transcribe(
    provider: TranscriptionProvider,
    audio: Path,
    language: str | None,
    video_id: uuid.UUID,
) -> TranscriptionResult:
    """Call the provider; raise JobError (or RetryableJobError) on failure."""
    log_extra = _video_log(video_id, provider=getattr(provider, "name", None))
    try:
        result = provider.transcribe(audio, language)
    except (InvalidTranscriptionResultError, InvalidLanguageError) as exc:
        logger.warning(
            "transcription_invalid_result",
            extra={**log_extra, "error_type": type(exc).__name__},
        )
        raise JobError(INVALID_RESULT_MESSAGE) from None
    except TranscriptionConfigurationError:
        logger.error("transcription_provider_misconfigured", extra=log_extra)
        raise JobError(PROVIDER_UNAVAILABLE_MESSAGE) from None
    except TranscriptionError as exc:
        # Provider messages are not logged: they can quote the request.
        logger.warning(
            "transcription_provider_failed",
            extra={**log_extra, "error_type": type(exc).__name__},
        )
        raise RetryableJobError(PROVIDER_FAILED_MESSAGE) from None
    if not isinstance(result, TranscriptionResult):
        logger.warning("transcription_invalid_result", extra=log_extra)
        raise JobError(INVALID_RESULT_MESSAGE)
    return result


def _clean_text(text: str) -> str:
    # PostgreSQL text cannot hold NUL characters.
    return text.replace("\x00", "")


def _fit_to_video(
    result: TranscriptionResult, duration: float | None, video_id: uuid.UUID
) -> TranscriptionResult:
    """Return the result checked against and clamped to the video's duration.

    Raises JobError if any timestamp is more than DURATION_TOLERANCE_SECONDS
    past the end. Segments left without length by clamping are dropped.
    """
    try:
        if duration is not None:
            result.check_within_duration(duration + DURATION_TOLERANCE_SECONDS)

        def clamp(value: float) -> float:
            return value if duration is None else min(value, duration)

        segments = []
        for segment in result.segments:
            start, end = clamp(segment.start), clamp(segment.end)
            if end <= start:
                continue
            words = None
            if segment.words is not None:
                words = tuple(
                    WordTiming(
                        word=_clean_text(word.word),
                        start=clamp(word.start),
                        end=clamp(word.end),
                    )
                    for word in segment.words
                    if _clean_text(word.word).strip()
                )
            segments.append(
                TranscriptionSegment(
                    start=start, end=end, text=_clean_text(segment.text), words=words
                )
            )
        return TranscriptionResult(
            language=result.language,
            segments=tuple(segments),
            provider=_clean_text(result.provider)[:PROVIDER_MAX_LENGTH],
            model=(
                None
                if result.model is None
                else _clean_text(result.model)[:MODEL_MAX_LENGTH] or None
            ),
        )
    except (InvalidTranscriptionResultError, TypeError, ValueError) as exc:
        logger.warning(
            "transcription_invalid_result",
            extra=_video_log(video_id, error_type=type(exc).__name__),
        )
        raise JobError(INVALID_RESULT_MESSAGE) from None


# Saving


def _save(
    db: Session,
    video_id: uuid.UUID,
    result: TranscriptionResult,
    requested_language: str | None,
) -> Transcript:
    """Replace the video's transcript in one transaction; roll back on failure."""
    try:
        video = _load_video(db, video_id, lock=True)
        if video is None:
            raise JobError(VIDEO_MISSING_MESSAGE)
        if video.status is not VideoStatus.READY:
            raise JobError(VIDEO_CHANGED_MESSAGE)
        transcript = replace_transcript(
            db, video_id, result, requested_language=requested_language
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return transcript


def run_transcription(
    db: Session,
    video_id: uuid.UUID,
    language: str | None = None,
    *,
    settings: Settings | None = None,
    storage: StorageBackend | None = None,
    provider: TranscriptionProvider | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> Transcript | None:
    """Transcribe the video and store the transcript; return it.

    Returns None without saving if `is_cancelled()` reports the job was
    cancelled. Raises JobError (or RetryableJobError) with a public message
    on failure; the previous transcript, if any, is then left unchanged.
    """
    settings = settings or get_settings()
    try:
        language = normalize_language(language)
    except InvalidLanguageError:
        raise JobError(INVALID_PAYLOAD_MESSAGE) from None

    video = _load_video(db, video_id)
    if video is None:
        db.rollback()
        raise JobError(VIDEO_MISSING_MESSAGE)
    if video.status is not VideoStatus.READY:
        db.rollback()
        raise JobError(VIDEO_NOT_READY_MESSAGE)
    user_id, key, duration = video.user_id, video.storage_key, video.duration_seconds
    # No transaction is held open during the long work below.
    db.rollback()

    if provider is None:
        try:
            provider = get_transcription_provider(settings)
        except TranscriptionConfigurationError:
            logger.error(
                "transcription_provider_unavailable", extra=_video_log(video_id)
            )
            raise JobError(PROVIDER_UNAVAILABLE_MESSAGE) from None

    logger.info(
        "transcription_started",
        extra=_video_log(video_id, provider=provider.name, requested_language=language),
    )
    # ignore_cleanup_errors: a file still held open (Windows) must not turn
    # a finished transcription into a failure.
    with tempfile.TemporaryDirectory(
        prefix=TEMP_DIR_PREFIX, ignore_cleanup_errors=True
    ) as workdir:
        audio = _extract_audio(video_id, user_id, key, storage, settings, Path(workdir))
        result = _transcribe(provider, audio, language, video_id)
    result = _fit_to_video(result, duration, video_id)

    if is_cancelled is not None and is_cancelled():
        logger.info("transcription_discarded", extra=_video_log(video_id))
        return None

    transcript = _save(db, video_id, result, language)
    logger.info(
        "transcription_completed",
        extra=_video_log(
            video_id,
            transcript_id=str(transcript.id),
            provider=result.provider,
            model=result.model,
            language=result.language,
            segment_count=len(result.segments),
        ),
    )
    return transcript


# Task and queuing


@job_task(
    JobType.TRANSCRIPTION,
    name=TRANSCRIBE_VIDEO_TASK_NAME,
    max_attempts=TRANSCRIPTION_MAX_ATTEMPTS,
    soft_time_limit=TRANSCRIPTION_SOFT_TIME_LIMIT_SECONDS,
    time_limit=TRANSCRIPTION_TIME_LIMIT_SECONDS,
)
def transcribe_video(ctx: JobContext) -> None:
    """Transcribe the video in `payload["video_id"]` (in `payload["language"]`)."""
    try:
        video_id = uuid.UUID(str(ctx.payload.get("video_id")))
    except ValueError:
        raise JobError(INVALID_PAYLOAD_MESSAGE) from None
    if ctx.video_id is not None and ctx.video_id != video_id:
        raise JobError(INVALID_PAYLOAD_MESSAGE)
    language = ctx.payload.get("language")
    if language is not None and not isinstance(language, str):
        raise JobError(INVALID_PAYLOAD_MESSAGE)
    with database.get_session_factory()() as db:
        run_transcription(db, video_id, language, is_cancelled=ctx.is_cancelled)


def get_active_transcription_job(
    db: Session, video_id: uuid.UUID
) -> ProcessingJob | None:
    """Return the video's queued or running transcription job, if any."""
    return db.scalars(
        select(ProcessingJob)
        .where(
            ProcessingJob.video_id == video_id,
            ProcessingJob.job_type == JobType.TRANSCRIPTION,
            ProcessingJob.status.in_(
                sorted(ACTIVE_JOB_STATUSES, key=lambda s: s.value)
            ),
        )
        .limit(1)
    ).first()


def queue_transcription(
    db: Session,
    video: Video,
    language: str | None = None,
    *,
    request_id: str | None = None,
) -> ProcessingJob:
    """Queue transcription of `video` and return its job.

    The video id is the idempotency key, so while a transcription job for the
    video is queued or running, that job is returned instead of a new one.
    Raises JobDispatchError if the job could not be published.
    """
    return enqueue_job(
        JobType.TRANSCRIPTION,
        transcribe_video,
        db=db,
        user_id=video.user_id,
        video_id=video.id,
        payload={"video_id": str(video.id), "language": normalize_language(language)},
        idempotency_key=str(video.id),
        request_id=request_id,
    )


__all__ = [
    "TRANSCRIBE_VIDEO_TASK_NAME",
    "get_active_transcription_job",
    "queue_transcription",
    "run_transcription",
    "transcribe_video",
]
