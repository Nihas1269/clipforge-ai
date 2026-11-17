"""Rendering an export of a clip (job type `render`).

queue_render() queues the render_export task for an export with the payload
`{"export_id": ...}`; the export id is the job's idempotency key, so at most
one render job per export is queued or running. The task runs run_render(),
which:

1. claims the export: a single conditional UPDATE moves it from `queued`
   (or `rendering`, for a retry of the same job) to `rendering` and records
   the job id. An export that is already `completed` is left as it is (a
   duplicate delivery does nothing); one owned by another active job, or
   `failed`, is refused. A queued export whose recorded job has ended
   without rendering it (for example cancelled while queued) is taken over;
2. checks the clip and its video (ready, with size and duration; the clip
   within the video; the source stored under the video's own key) and loads
   the captions and caption style when `include_captions` is set;
3. in a new temporary directory, plans the reframe (app.reframe.plan_reframe;
   face mode falls back to a center crop), writes the captions as an ASS
   file, and runs FFmpeg with the arguments from
   app.render.pipeline.build_ffmpeg_args;
4. validates the output with ffprobe (non-empty, playable, the output size of
   the aspect ratio, duration within DURATION_TOLERANCE_SECONDS of the clip
   length, an audio stream when the source has one). A file that fails is a
   failed render, never a success;
5. only then uploads it to `users/{user_id}/exports/{export_id}.mp4` and, in
   one conditional UPDATE, marks the export `completed` with its key and size.

The temporary directory is removed on success and on every failure. If the
upload or the final update fails, the stored object is deleted again, so a
failed export never leaves a file behind. Retries write to the same key, so
they never duplicate anything.

Failures: storage read/write errors and local disk errors are retried
(RetryableJobError); everything else, including a soft time limit during the
upload, fails at once. When the job fails for good (no attempts
left, a non-retryable error, a time limit, an unexpected error or a
cancellation), the export is marked `failed` with the same sanitized message
as the job (app.worker.jobs.sanitize_error_message): never paths, keys,
FFmpeg output or caption text. If the job fails without the export being
marked (its worker was lost, or a retry could not be published), the
periodic app.worker.jobs.fail_stale_jobs fails the export left `rendering`.
"""

import logging
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import ColumnElement, and_, or_, select, update
from sqlalchemy.orm import Session

from app import database
from app.captions.ass import render_ass
from app.captions.styles import StyleValues, default_style
from app.core.config import Settings, get_settings
from app.media.ffmpeg import (
    BYTES_PER_MB,
    FFmpegNotFoundError,
    MediaError,
    MediaTimeoutError,
    probe,
    run_ffmpeg,
)
from app.models.caption import CaptionStyle, ClipCaption
from app.models.clip import Clip
from app.models.export import ACTIVE_EXPORT_STATUSES, Export, ExportStatus
from app.models.processing_job import ACTIVE_JOB_STATUSES, JobType, ProcessingJob
from app.models.video import Video, VideoStatus
from app.reframe import ReframeError, plan_reframe
from app.render.pipeline import (
    RenderValidationError,
    build_ffmpeg_args,
    check_render_output,
    output_resolution,
)
from app.storage import (
    InvalidStorageKeyError,
    StorageBackend,
    StorageConfigurationError,
    StorageError,
    StorageObjectNotFoundError,
    StorageObjectTooLargeError,
    export_key,
    get_storage,
)
from app.worker.jobs import (
    JobContext,
    JobError,
    RetryableJobError,
    enqueue_job,
    job_task,
    sanitize_error_message,
)

logger = logging.getLogger(__name__)

RENDER_EXPORT_TASK_NAME = "app.worker.render.render_export"
RENDER_MAX_ATTEMPTS = 3
# Covers copying the source from remote storage, face tracking (at most
# 300 s by default), FFmpeg (FFMPEG_TIMEOUT_SECONDS, 1800 s by default),
# probing and the upload; the hard limit may not exceed
# jobs.MAX_TIME_LIMIT_SECONDS.
RENDER_SOFT_TIME_LIMIT_SECONDS = 3300
RENDER_TIME_LIMIT_SECONDS = 3420
TEMP_DIR_PREFIX = "clipforge-render-"
OUTPUT_FILENAME = "output.mp4"
SUBTITLES_FILENAME = "captions.ass"

# Messages stored on the job and the export. They are shown to the owner, so
# they never contain paths, keys, tool output or caption text.
INVALID_PAYLOAD_MESSAGE = "The render job is invalid."
EXPORT_MISSING_MESSAGE = "The export no longer exists."
EXPORT_NOT_RENDERABLE_MESSAGE = "The export cannot be rendered again."
CLIP_MISSING_MESSAGE = "The clip no longer exists."
VIDEO_NOT_READY_MESSAGE = "The video is not ready for rendering."
CLIP_OUT_OF_RANGE_MESSAGE = "The clip is outside the video."
STORAGE_UNAVAILABLE_MESSAGE = "File storage is not available."
FILE_MISSING_MESSAGE = "The uploaded file could not be found."
FILE_UNREADABLE_MESSAGE = "The uploaded file could not be read."
INVALID_LOCATION_MESSAGE = "The uploaded file is not stored where expected."
FFMPEG_UNAVAILABLE_MESSAGE = (
    "Rendering is not available because FFmpeg is not installed or configured "
    "on the server."
)
RENDER_TIMEOUT_MESSAGE = "Rendering the video took too long."
RENDER_FAILED_MESSAGE = "The video could not be rendered."
REFRAME_FAILED_MESSAGE = "The video could not be reframed."
CAPTIONS_FAILED_MESSAGE = "The captions could not be prepared for rendering."
INVALID_OUTPUT_MESSAGE = "The rendered video failed validation."
UPLOAD_FAILED_MESSAGE = "The rendered video could not be stored. Try again later."
OUTPUT_TOO_LARGE_MESSAGE = "The rendered video is too large to store."
QUEUE_FAILED_MESSAGE = "The render could not be queued. Try again later."
EXPORT_CHANGED_MESSAGE = "The export changed while it was being rendered."
CANCELLED_MESSAGE = "The export was cancelled."


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _export_log(export_id: uuid.UUID, **extra: object) -> dict[str, object]:
    return {"export_id": str(export_id), **extra}


@dataclass
class _Caption:
    start_seconds: float
    end_seconds: float
    text: str


@dataclass(frozen=True)
class _RenderInput:
    """Everything the render needs, read before the long work starts."""

    export_id: uuid.UUID
    user_id: uuid.UUID
    video_id: uuid.UUID
    source_key: str
    source_width: int
    source_height: int
    video_duration: float
    start: float
    end: float
    aspect_ratio: str
    reframe_mode: str
    captions: tuple[_Caption, ...]
    style: StyleValues | None


# Export state changes


def _recorded_job_ended() -> ColumnElement[bool]:
    """SQL condition: the export's recorded job exists and is no longer active.

    Such a job (for example one cancelled while queued) can never render the
    export, so a queued export it left behind may be taken over.
    """
    return (
        select(ProcessingJob.id)
        .where(
            ProcessingJob.id == Export.job_id,
            ProcessingJob.status.not_in(
                sorted(ACTIVE_JOB_STATUSES, key=lambda s: s.value)
            ),
        )
        .correlate(Export)
        .exists()
    )


def _claim(db: Session, export_id: uuid.UUID, job_id: uuid.UUID) -> Export | None:
    """Move the export to `rendering` for `job_id`; return it, or None if done.

    Returns None (nothing to do) when the export is already completed.
    Raises JobError when it is missing, failed, rendering for another job, or
    queued for another job that is still active.
    """
    claimed = db.execute(
        update(Export)
        .where(
            Export.id == export_id,
            Export.status.in_(sorted(ACTIVE_EXPORT_STATUSES, key=lambda s: s.value)),
            or_(
                Export.job_id.is_(None),
                Export.job_id == job_id,
                and_(Export.status == ExportStatus.QUEUED, _recorded_job_ended()),
            ),
        )
        .values(status=ExportStatus.RENDERING, job_id=job_id, error_message=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    export = db.scalars(
        select(Export)
        .where(Export.id == export_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if export is None:
        raise JobError(EXPORT_MISSING_MESSAGE)
    if getattr(claimed, "rowcount", 0) == 1:
        return export
    if export.status is ExportStatus.COMPLETED:
        logger.info("render_already_completed", extra=_export_log(export_id))
        return None
    logger.warning(
        "render_claim_refused",
        extra=_export_log(export_id, status=export.status.value),
    )
    raise JobError(EXPORT_NOT_RENDERABLE_MESSAGE)


def _complete(
    db: Session, export_id: uuid.UUID, job_id: uuid.UUID, key: str, size: int
) -> bool:
    """Mark the export completed if this job still owns it; commit."""
    try:
        changed = db.execute(
            update(Export)
            .where(
                Export.id == export_id,
                Export.status == ExportStatus.RENDERING,
                Export.job_id == job_id,
            )
            .values(
                status=ExportStatus.COMPLETED,
                storage_key=key,
                size_bytes=size,
                error_message=None,
                completed_at=_utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return getattr(changed, "rowcount", 0) == 1


def mark_export_failed(
    db: Session, export_id: uuid.UUID, message: str, *, job_id: uuid.UUID | None
) -> bool:
    """Mark a queued or rendering export failed with a public `message`.

    With `job_id`, only an export owned by that job (or not yet claimed) is
    changed. Commits and returns whether the export was changed.
    """
    conditions: list[ColumnElement[bool]] = [
        Export.status.in_(sorted(ACTIVE_EXPORT_STATUSES, key=lambda s: s.value))
    ]
    if job_id is not None:
        conditions.append(or_(Export.job_id.is_(None), Export.job_id == job_id))
    return _fail_export(db, export_id, message, *conditions)


def _fail_unclaimed(db: Session, export_id: uuid.UUID, message: str) -> bool:
    """Mark a queued export failed unless an active job has recorded it.

    Used when queuing fails: an export that a concurrent request queued (or
    whose job already claimed it) is left to that job. Commits and returns
    whether the export was changed.
    """
    return _fail_export(
        db,
        export_id,
        message,
        Export.status == ExportStatus.QUEUED,
        or_(Export.job_id.is_(None), _recorded_job_ended()),
    )


def _fail_export(
    db: Session, export_id: uuid.UUID, message: str, *conditions: ColumnElement[bool]
) -> bool:
    statement = update(Export).where(Export.id == export_id, *conditions)
    try:
        changed = db.execute(
            statement.values(
                status=ExportStatus.FAILED,
                error_message=message,
                storage_key=None,
                size_bytes=None,
            ).execution_options(synchronize_session=False)
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return getattr(changed, "rowcount", 0) == 1


# Loading


def _style_values(style: CaptionStyle | None, settings: Settings) -> StyleValues:
    if style is None:
        return default_style(settings.CAPTION_FONTS[0])
    return StyleValues(
        preset=style.preset,
        font_family=style.font_family,
        font_size=style.font_size,
        primary_color=style.primary_color,
        outline_color=style.outline_color,
        position=style.position,
        uppercase=style.uppercase,
        max_chars_per_line=style.max_chars_per_line,
    )


def _load_input(db: Session, export: Export, settings: Settings) -> _RenderInput:
    """Read and check the clip, video and captions; raise JobError if unusable."""
    clip = db.get(Clip, export.clip_id, populate_existing=True)
    if clip is None:
        raise JobError(CLIP_MISSING_MESSAGE)
    video = db.get(Video, clip.video_id, populate_existing=True)
    if video is None or video.user_id != export.user_id:
        # An export always belongs to the owner of the clip's video.
        raise JobError(CLIP_MISSING_MESSAGE)
    width, height, duration = video.width, video.height, video.duration_seconds
    if video.status is not VideoStatus.READY or not width or not height or not duration:
        raise JobError(VIDEO_NOT_READY_MESSAGE)
    if not 0 <= clip.start_seconds < clip.end_seconds <= duration:
        raise JobError(CLIP_OUT_OF_RANGE_MESSAGE)
    # The key is generated from the ids at upload; anything else is refused.
    if not isinstance(video.storage_key, str) or not video.storage_key.startswith(
        f"users/{video.user_id}/videos/{video.id}/"
    ):
        logger.error("render_unexpected_key", extra=_export_log(export.id))
        raise JobError(INVALID_LOCATION_MESSAGE)

    captions: tuple[_Caption, ...] = ()
    style = None
    if export.include_captions:
        rows = db.scalars(
            select(ClipCaption)
            .where(ClipCaption.clip_id == clip.id)
            .order_by(ClipCaption.index)
        ).all()
        captions = tuple(
            _Caption(row.start_seconds, row.end_seconds, row.text) for row in rows
        )
        style_row = db.scalars(
            select(CaptionStyle).where(CaptionStyle.clip_id == clip.id)
        ).one_or_none()
        style = _style_values(style_row, settings)

    return _RenderInput(
        export_id=export.id,
        user_id=export.user_id,
        video_id=video.id,
        source_key=video.storage_key,
        source_width=width,
        source_height=height,
        video_duration=duration,
        start=clip.start_seconds,
        end=clip.end_seconds,
        aspect_ratio=export.aspect_ratio.value,
        reframe_mode=export.reframe_mode.value,
        captions=captions,
        style=style,
    )


# Rendering


def _write_subtitles(data: _RenderInput, workdir: Path) -> Path | None:
    """Write the clip's captions as an ASS file; None when there are none."""
    if data.style is None or not data.captions:
        return None
    width, height = output_resolution(data.aspect_ratio)
    try:
        # Caption times are relative to the clip start, and the rendered
        # video starts at 0 at the clip start, so no offset is applied.
        content = render_ass(data.captions, data.style, width, height)
    except ValueError:
        logger.warning("render_captions_invalid", extra=_export_log(data.export_id))
        raise JobError(CAPTIONS_FAILED_MESSAGE) from None
    path = workdir / SUBTITLES_FILENAME
    try:
        path.write_text(content, encoding="utf-8", newline="\n")
    except OSError as exc:
        # A local disk problem (for example a full disk) may be temporary.
        logger.warning(
            "render_captions_write_failed",
            extra=_export_log(data.export_id, error_type=type(exc).__name__),
        )
        raise RetryableJobError(CAPTIONS_FAILED_MESSAGE) from None
    return path


def _render_file(
    data: _RenderInput,
    storage: StorageBackend,
    settings: Settings,
    workdir: Path,
) -> Path:
    """Render the clip into `workdir` and validate it; return the output path."""
    output = workdir / OUTPUT_FILENAME
    max_bytes = settings.FFMPEG_MAX_OUTPUT_FILE_MB * BYTES_PER_MB
    with storage.local_path(data.source_key) as source:
        source_info = probe(source, settings=settings)
        try:
            plan = plan_reframe(
                source,
                data.start,
                data.end,
                data.aspect_ratio,
                data.reframe_mode,
                source_size=(data.source_width, data.source_height),
                media_duration=data.video_duration,
            )
        except (ReframeError, ValueError) as exc:
            logger.warning(
                "render_reframe_failed",
                extra=_export_log(data.export_id, error_type=type(exc).__name__),
            )
            raise JobError(REFRAME_FAILED_MESSAGE) from None
        subtitles = _write_subtitles(data, workdir)
        args = build_ffmpeg_args(
            Path(source).resolve(),
            output.resolve(),
            start=data.start,
            end=data.end,
            plan=plan,
            subtitles_path=subtitles.resolve() if subtitles is not None else None,
            max_output_bytes=max_bytes,
        )
        logger.info(
            "render_started",
            extra=_export_log(
                data.export_id,
                aspect_ratio=data.aspect_ratio,
                requested_mode=data.reframe_mode,
                mode=plan.mode,
                fallback_reason=plan.fallback_reason,
                keyframes=len(plan.keyframes),
                captions=len(data.captions) if subtitles is not None else 0,
            ),
        )
        run_ffmpeg(args, settings=settings)

    width, height = output_resolution(data.aspect_ratio)
    if not output.is_file():
        raise RenderValidationError("missing", "FFmpeg did not write the output.")
    size = output.stat().st_size
    if size >= max_bytes:
        # FFmpeg stopped at the -fs limit, so the file is cut short.
        logger.warning("render_output_too_large", extra=_export_log(data.export_id))
        raise JobError(OUTPUT_TOO_LARGE_MESSAGE)
    check_render_output(
        probe(output, settings=settings),
        size_bytes=size,
        width=width,
        height=height,
        duration=data.end - data.start,
        expect_audio=source_info.has_audio,
    )
    return output


def _render(
    data: _RenderInput, storage: StorageBackend, settings: Settings, workdir: Path
) -> Path:
    """_render_file() with failures turned into public JobErrors."""
    log_extra = _export_log(data.export_id)
    try:
        return _render_file(data, storage, settings, workdir)
    except FFmpegNotFoundError:
        logger.error("render_ffmpeg_missing", extra=log_extra)
        raise JobError(FFMPEG_UNAVAILABLE_MESSAGE) from None
    except MediaTimeoutError:
        logger.warning("render_timeout", extra=log_extra)
        raise JobError(RENDER_TIMEOUT_MESSAGE) from None
    except MediaError:
        # The tool output is not logged: it can echo paths and caption text.
        logger.warning("render_ffmpeg_failed", extra=log_extra)
        raise JobError(RENDER_FAILED_MESSAGE) from None
    except RenderValidationError as exc:
        logger.warning(
            "render_output_invalid", extra={**log_extra, "reason": exc.reason}
        )
        raise JobError(INVALID_OUTPUT_MESSAGE) from None
    except StorageObjectNotFoundError:
        logger.warning("render_file_missing", extra=log_extra)
        raise JobError(FILE_MISSING_MESSAGE) from None
    except InvalidStorageKeyError:
        logger.error("render_invalid_key", extra=log_extra)
        raise JobError(INVALID_LOCATION_MESSAGE) from None
    except (OSError, StorageError, BotoCoreError, ClientError) as exc:
        # Network or disk errors while reading the source may be temporary.
        logger.warning(
            "render_read_failed",
            extra={**log_extra, "error_type": type(exc).__name__},
        )
        raise RetryableJobError(FILE_UNREADABLE_MESSAGE) from None


def _delete_quietly(storage: StorageBackend, key: str, export_id: uuid.UUID) -> None:
    try:
        storage.delete(key)
    except Exception as exc:
        logger.error(
            "render_cleanup_failed",
            extra=_export_log(export_id, error_type=type(exc).__name__),
        )


def _check_temp_dir_removed(path: Path, export_id: uuid.UUID) -> None:
    """Log a temporary directory that could not be removed (cleanup is quiet)."""
    if path.exists():
        logger.error("render_temp_cleanup_failed", extra=_export_log(export_id))


def _store(
    db: Session,
    data: _RenderInput,
    job_id: uuid.UUID,
    output: Path,
    storage: StorageBackend,
    settings: Settings,
) -> tuple[str, int]:
    """Upload the validated output and complete the export; clean up on failure."""
    key = export_key(str(data.user_id), str(data.export_id))
    try:
        with output.open("rb") as fileobj:
            size = storage.save(
                key,
                fileobj,
                max_bytes=settings.FFMPEG_MAX_OUTPUT_FILE_MB * BYTES_PER_MB,
            )
    except BaseException as exc:
        # A failed upload must not leave a partial object behind.
        _delete_quietly(storage, key, data.export_id)
        if isinstance(exc, StorageObjectTooLargeError):
            logger.warning("render_output_too_large", extra=_export_log(data.export_id))
            raise JobError(OUTPUT_TOO_LARGE_MESSAGE) from None
        if isinstance(exc, (OSError, StorageError, BotoCoreError, ClientError)):
            logger.warning(
                "render_upload_failed",
                extra=_export_log(data.export_id, error_type=type(exc).__name__),
            )
            raise RetryableJobError(UPLOAD_FAILED_MESSAGE) from None
        # Anything else (a soft time limit, an unexpected error) fails the
        # job for good rather than retrying the whole render.
        raise

    try:
        completed = _complete(db, data.export_id, job_id, key, size)
    except BaseException:
        _delete_quietly(storage, key, data.export_id)
        raise
    if not completed:
        # Deleted, cancelled or taken over meanwhile: keep nothing.
        _delete_quietly(storage, key, data.export_id)
        raise JobError(EXPORT_CHANGED_MESSAGE)
    return key, size


def run_render(
    db: Session,
    export_id: uuid.UUID,
    job_id: uuid.UUID,
    *,
    settings: Settings | None = None,
    storage: StorageBackend | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> Export | None:
    """Render the export for job `job_id`, store it and mark it completed.

    Returns the completed export, or None when there was nothing to do (the
    export was already completed). Raises JobError (or RetryableJobError)
    with a public message on failure; the export's status is then left to
    the caller (see render_export), and nothing is stored.
    """
    settings = settings or get_settings()
    export = _claim(db, export_id, job_id)
    if export is None:
        return None
    try:
        data = _load_input(db, export, settings)
    finally:
        # No transaction is held open during the long work below.
        db.rollback()

    if storage is None:
        try:
            storage = get_storage(settings)
        except StorageConfigurationError:
            logger.error("render_storage_unavailable", extra=_export_log(export_id))
            raise JobError(STORAGE_UNAVAILABLE_MESSAGE) from None

    # ignore_cleanup_errors: a file still held open (Windows) must not turn a
    # finished render into a failure; what is left behind is logged instead.
    temp_dir = tempfile.TemporaryDirectory(
        prefix=TEMP_DIR_PREFIX, ignore_cleanup_errors=True
    )
    try:
        with temp_dir as workdir:
            output = _render(data, storage, settings, Path(workdir))
            if is_cancelled is not None and is_cancelled():
                logger.info("render_discarded", extra=_export_log(export_id))
                raise JobError(CANCELLED_MESSAGE)
            key, size = _store(db, data, job_id, output, storage, settings)
    finally:
        _check_temp_dir_removed(Path(temp_dir.name), export_id)

    logger.info(
        "render_completed",
        extra=_export_log(
            export_id,
            aspect_ratio=data.aspect_ratio,
            size_bytes=size,
            duration_seconds=round(data.end - data.start, 3),
        ),
    )
    return db.get(Export, export_id, populate_existing=True)


# Task and queuing


def _is_final_failure(exc: Exception, ctx: JobContext) -> bool:
    """True if the job wrapper will fail the job rather than retry it."""
    return not (
        isinstance(exc, JobError) and exc.retryable and ctx.attempt < ctx.max_attempts
    )


@job_task(
    JobType.RENDER,
    name=RENDER_EXPORT_TASK_NAME,
    max_attempts=RENDER_MAX_ATTEMPTS,
    soft_time_limit=RENDER_SOFT_TIME_LIMIT_SECONDS,
    time_limit=RENDER_TIME_LIMIT_SECONDS,
)
def render_export(ctx: JobContext) -> None:
    """Render the export in `payload["export_id"]`."""
    try:
        export_id = uuid.UUID(str(ctx.payload.get("export_id")))
    except ValueError:
        raise JobError(INVALID_PAYLOAD_MESSAGE) from None
    with database.get_session_factory()() as db:
        try:
            run_render(db, export_id, ctx.job_id, is_cancelled=ctx.is_cancelled)
        except Exception as exc:
            db.rollback()
            if _is_final_failure(exc, ctx):
                # The same sanitized, bounded message as the job's.
                mark_export_failed(
                    db, export_id, sanitize_error_message(exc), job_id=ctx.job_id
                )
                logger.warning(
                    "render_failed",
                    extra=_export_log(export_id, error_type=type(exc).__name__),
                )
            raise


def queue_render(
    db: Session, export: Export, *, request_id: str | None = None
) -> ProcessingJob:
    """Queue rendering of `export` and return its job.

    The export id is the idempotency key, so while a render job for the
    export is queued or running, that job is returned instead of a new one.
    Raises ValueError for an export that is not queued, and JobDispatchError
    if the job could not be published (the export is then marked failed,
    unless an active job has recorded or claimed it meanwhile).
    """
    if export.status is not ExportStatus.QUEUED:
        raise ValueError("Only a queued export can be rendered.")
    clip = db.get(Clip, export.clip_id)
    video_id = clip.video_id if clip is not None else None
    export_id = export.id
    try:
        job = enqueue_job(
            JobType.RENDER,
            render_export,
            db=db,
            user_id=export.user_id,
            video_id=video_id,
            payload={"export_id": str(export_id)},
            idempotency_key=str(export_id),
            request_id=request_id,
        )
    except Exception:
        db.rollback()
        _fail_unclaimed(db, export_id, QUEUE_FAILED_MESSAGE)
        raise
    # Records the job on an export the worker has not claimed yet (an eager
    # or fast worker may already have done so with the same id). An export
    # whose earlier job ended without rendering it (for example cancelled
    # while queued) is taken over by the new job.
    db.execute(
        update(Export)
        .where(
            Export.id == export_id,
            Export.status == ExportStatus.QUEUED,
            or_(Export.job_id.is_(None), _recorded_job_ended()),
        )
        .values(job_id=job.id)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.refresh(export)
    return job


__all__ = [
    "RENDER_EXPORT_TASK_NAME",
    "mark_export_failed",
    "queue_render",
    "render_export",
    "run_render",
]
