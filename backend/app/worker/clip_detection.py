"""Clip detection for a transcribed video (job type `clip_detection`).

`POST /videos/{id}/clips/detect` calls queue_clip_detection(), which queues
the detect_clips task with the payload `{"video_id": ...}`. The task runs
run_clip_detection(), which:

1. checks that the video exists, is `ready`, has a duration and a transcript;
2. runs the detector selected by CLIP_DETECTOR (get_clip_detector) on the
   transcript segments, with the CLIP_* length and count limits;
3. validates the result again with validate_candidates() (whatever the
   detector, every clip satisfies 0 <= start < end <= duration, the length
   limits, and clips do not overlap);
4. in one transaction, with the video row locked, deletes the video's
   detected clips that are still `suggested` and inserts the new
   suggestions (app.repositories.clips.replace_suggestions). Accepted,
   rejected and manual clips are never touched.

The video id is the job's idempotency key, so at most one detection job per
video is queued or running and two jobs can never overwrite each other's
suggestions. Until step 4 commits the previous suggestions stay as they
were; a failure while saving is rolled back. A cancelled job does not save.

Each clip records its source (`heuristic` or `llm`) and the detector
version, and for model-based detectors the model and prompt version. When
the LLM detector falls back to the heuristic detector (its `last_run` names
the detector actually used), the clips record the heuristic provenance.
An unknown CLIP_DETECTOR name uses the heuristic detector (see
app.clips.get_clip_detector).

Failures raise JobError with a message that is safe to show to the owner;
it never contains transcript text. Detector I/O errors (only possible for
remote detectors) are retried.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import database
from app.clips import (
    ClipDetectionOptions,
    ClipDetectionRun,
    ClipDetector,
    InvalidClipCandidateError,
    get_clip_detector,
    validate_candidates,
)
from app.clips.base import ClipCandidate
from app.core.config import Settings, get_settings
from app.models.clip import (
    DETECTOR_MODEL_MAX_LENGTH,
    DETECTOR_VERSION_MAX_LENGTH,
    PROMPT_VERSION_MAX_LENGTH,
    Clip,
    ClipSource,
)
from app.models.processing_job import ACTIVE_JOB_STATUSES, JobType, ProcessingJob
from app.models.video import Video, VideoStatus
from app.repositories.clips import replace_suggestions
from app.repositories.transcripts import get_transcript_for_video
from app.worker.jobs import (
    JobContext,
    JobError,
    RetryableJobError,
    enqueue_job,
    job_task,
)

logger = logging.getLogger(__name__)

DETECT_CLIPS_TASK_NAME = "app.worker.clip_detection.detect_clips"
CLIP_DETECTION_MAX_ATTEMPTS = 3
CLIP_DETECTION_SOFT_TIME_LIMIT_SECONDS = 600
CLIP_DETECTION_TIME_LIMIT_SECONDS = 660

# Messages stored on the job. They are shown to the video's owner, so they
# never contain transcript text, settings or exception details.
INVALID_PAYLOAD_MESSAGE = "The clip detection job is invalid."
VIDEO_MISSING_MESSAGE = "The video no longer exists."
VIDEO_NOT_READY_MESSAGE = "The video is not ready for clip detection."
VIDEO_CHANGED_MESSAGE = "The video changed while clips were being detected. Try again."
NO_TRANSCRIPT_MESSAGE = "The video has no transcript. Transcribe it first."
DETECTOR_UNAVAILABLE_MESSAGE = "Clip detection is not available on the server."
DETECTOR_FAILED_MESSAGE = "Clip detection failed. Try again later."
INVALID_RESULT_MESSAGE = "Clip detection returned an invalid result."


@dataclass(frozen=True)
class _Span:
    """A transcript segment as the detectors see it (source-video seconds)."""

    start: float
    end: float
    text: str


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
        # Serializes the replacement with deletion and other writers on
        # PostgreSQL.
        statement = statement.with_for_update()
    return db.scalars(statement).one_or_none()


def _clean_label(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.replace("\x00", "").strip()[:limit]
    return value or None


def _optional_label(detector: object, attribute: str, limit: int) -> str | None:
    return _clean_label(getattr(detector, attribute, None), limit)


@dataclass(frozen=True)
class _Provenance:
    """What produced a run's clips, as stored on each clip."""

    source: ClipSource
    detector_version: str | None
    detector_model: str | None
    prompt_version: str | None


def _detector_source(name: object, video_id: uuid.UUID) -> ClipSource:
    try:
        source = ClipSource(name)
    except ValueError:
        source = ClipSource.MANUAL
    if source is ClipSource.MANUAL:
        logger.error("clip_detector_unknown_source", extra=_video_log(video_id))
        raise JobError(DETECTOR_UNAVAILABLE_MESSAGE)
    return source


def _provenance(detector: ClipDetector, video_id: uuid.UUID) -> _Provenance:
    """Provenance of the clips the detector's last detect() call returned."""
    source = _detector_source(getattr(detector, "name", None), video_id)
    run = getattr(detector, "last_run", None)
    if not isinstance(run, ClipDetectionRun):
        return _Provenance(
            source=source,
            detector_version=_optional_label(
                detector, "version", DETECTOR_VERSION_MAX_LENGTH
            ),
            detector_model=_optional_label(
                detector, "model", DETECTOR_MODEL_MAX_LENGTH
            ),
            prompt_version=_optional_label(
                detector, "prompt_version", PROMPT_VERSION_MAX_LENGTH
            ),
        )
    if run.fallback_used or run.detector != source.value:
        # The clips came from the fallback detector, not the model.
        fallback = getattr(detector, "fallback", None)
        return _Provenance(
            source=_detector_source(run.detector, video_id),
            detector_version=_optional_label(
                fallback, "version", DETECTOR_VERSION_MAX_LENGTH
            ),
            detector_model=None,
            prompt_version=None,
        )
    return _Provenance(
        source=source,
        detector_version=_optional_label(
            detector, "version", DETECTOR_VERSION_MAX_LENGTH
        )
        or source.value,
        detector_model=_clean_label(run.model, DETECTOR_MODEL_MAX_LENGTH),
        prompt_version=_clean_label(run.prompt_version, PROMPT_VERSION_MAX_LENGTH),
    )


def _detect(
    detector: ClipDetector,
    spans: list[_Span],
    duration: float,
    options: ClipDetectionOptions,
    video_id: uuid.UUID,
) -> list[ClipCandidate]:
    """Run the detector and re-validate its output; raise JobError on failure."""
    log_extra = _video_log(video_id, detector=getattr(detector, "name", None))
    try:
        candidates = detector.detect(spans, duration, options)
        # The same strict checks for every detector, including future
        # model-based ones whose output cannot be trusted.
        return validate_candidates(candidates, duration, options)
    except (InvalidClipCandidateError, TypeError, ValueError) as exc:
        logger.warning(
            "clip_detection_invalid_result",
            extra={**log_extra, "error_type": type(exc).__name__},
        )
        raise JobError(INVALID_RESULT_MESSAGE) from None
    except OSError as exc:
        # Network or timeout errors of a remote detector may be temporary.
        logger.warning(
            "clip_detection_detector_failed",
            extra={**log_extra, "error_type": type(exc).__name__},
        )
        raise RetryableJobError(DETECTOR_FAILED_MESSAGE) from None


def _save(
    db: Session,
    video_id: uuid.UUID,
    duration: float,
    candidates: list[ClipCandidate],
    provenance: _Provenance,
) -> list[Clip]:
    """Replace the suggestions in one transaction; roll back on failure."""
    try:
        video = _load_video(db, video_id, lock=True)
        if video is None:
            raise JobError(VIDEO_MISSING_MESSAGE)
        if video.status is not VideoStatus.READY or video.duration_seconds != duration:
            raise JobError(VIDEO_CHANGED_MESSAGE)
        clips = replace_suggestions(
            db,
            video_id,
            candidates,
            source=provenance.source,
            detector_version=provenance.detector_version,
            detector_model=provenance.detector_model,
            prompt_version=provenance.prompt_version,
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return clips


def run_clip_detection(
    db: Session,
    video_id: uuid.UUID,
    *,
    settings: Settings | None = None,
    detector: ClipDetector | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> list[Clip] | None:
    """Detect clips for the video and store them as suggestions; return them.

    Returns None without saving if `is_cancelled()` reports the job was
    cancelled. Raises JobError (or RetryableJobError) with a public message
    on failure; the existing clips are then left unchanged.
    """
    settings = settings or get_settings()
    video = _load_video(db, video_id)
    if video is None:
        db.rollback()
        raise JobError(VIDEO_MISSING_MESSAGE)
    duration = video.duration_seconds
    if video.status is not VideoStatus.READY or not duration or duration <= 0:
        db.rollback()
        raise JobError(VIDEO_NOT_READY_MESSAGE)
    transcript = get_transcript_for_video(db, video_id)
    if transcript is None:
        db.rollback()
        raise JobError(NO_TRANSCRIPT_MESSAGE)
    spans = [
        _Span(start=s.start_seconds, end=s.end_seconds, text=s.text)
        for s in transcript.segments
    ]
    # No transaction is held open while the detector runs.
    db.rollback()

    if detector is None:
        detector = get_clip_detector(settings)
    source = _detector_source(getattr(detector, "name", None), video_id)

    options = ClipDetectionOptions.from_settings(settings)
    logger.info(
        "clip_detection_started",
        extra=_video_log(video_id, detector=source.value, segment_count=len(spans)),
    )
    candidates = _detect(detector, spans, duration, options, video_id)

    if is_cancelled is not None and is_cancelled():
        logger.info("clip_detection_discarded", extra=_video_log(video_id))
        return None

    provenance = _provenance(detector, video_id)
    clips = _save(db, video_id, duration, candidates, provenance)
    logger.info(
        "clip_detection_saved",
        extra=_video_log(
            video_id,
            detector=provenance.source.value,
            candidate_count=len(candidates),
            clip_count=len(clips),
        ),
    )
    return clips


# Task and queuing


@job_task(
    JobType.CLIP_DETECTION,
    name=DETECT_CLIPS_TASK_NAME,
    max_attempts=CLIP_DETECTION_MAX_ATTEMPTS,
    soft_time_limit=CLIP_DETECTION_SOFT_TIME_LIMIT_SECONDS,
    time_limit=CLIP_DETECTION_TIME_LIMIT_SECONDS,
)
def detect_clips(ctx: JobContext) -> None:
    """Detect clips for the video in `payload["video_id"]`."""
    try:
        video_id = uuid.UUID(str(ctx.payload.get("video_id")))
    except ValueError:
        raise JobError(INVALID_PAYLOAD_MESSAGE) from None
    if ctx.video_id is not None and ctx.video_id != video_id:
        raise JobError(INVALID_PAYLOAD_MESSAGE)
    with database.get_session_factory()() as db:
        run_clip_detection(db, video_id, is_cancelled=ctx.is_cancelled)


def get_active_clip_detection_job(
    db: Session, video_id: uuid.UUID
) -> ProcessingJob | None:
    """Return the video's queued or running clip detection job, if any."""
    return db.scalars(
        select(ProcessingJob)
        .where(
            ProcessingJob.video_id == video_id,
            ProcessingJob.job_type == JobType.CLIP_DETECTION,
            ProcessingJob.status.in_(
                sorted(ACTIVE_JOB_STATUSES, key=lambda s: s.value)
            ),
        )
        .limit(1)
    ).first()


def queue_clip_detection(
    db: Session, video: Video, *, request_id: str | None = None
) -> ProcessingJob:
    """Queue clip detection for `video` and return its job.

    The video id is the idempotency key, so while a detection job for the
    video is queued or running, that job is returned instead of a new one.
    Raises JobDispatchError if the job could not be published.
    """
    return enqueue_job(
        JobType.CLIP_DETECTION,
        detect_clips,
        db=db,
        user_id=video.user_id,
        video_id=video.id,
        payload={"video_id": str(video.id)},
        idempotency_key=str(video.id),
        request_id=request_id,
    )


__all__ = [
    "DETECT_CLIPS_TASK_NAME",
    "detect_clips",
    "get_active_clip_detection_job",
    "queue_clip_detection",
    "run_clip_detection",
]
