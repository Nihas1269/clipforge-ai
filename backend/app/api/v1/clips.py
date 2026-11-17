"""Clip detection jobs and clip review.

Every endpoint requires authentication and works only on the current user's
videos; ownership of a clip is checked through its parent video
(get_owned_video / get_owned_clip), and another user's video or clip gets
the same 404 as a missing one.

- `POST /videos/{id}/clips/detect` queues a `clip_detection` job (see
  app.worker.clip_detection) and returns 202 with its id. The video must be
  `ready` and transcribed, and no detection job may be queued or running
  for it (409 otherwise).
- `GET /videos/{id}/clips` lists the clips, best score first, then earlier
  start (then earlier end and id), optionally filtered by status.
- `POST /videos/{id}/clips` creates a manual clip. It is `accepted`, has
  score 1 and is never replaced by detection.
- `PATCH /clips/{id}` changes the range, title or status. The range must
  satisfy 0 <= start < end <= the video's duration and the CLIP_MIN_SECONDS /
  CLIP_MAX_SECONDS length limits (422 otherwise). Status changes follow
  ALLOWED_CLIP_STATUS_TRANSITIONS (409 otherwise).
- `DELETE /clips/{id}` deletes a clip.

Times are always seconds of source-video time.
"""

import logging

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_owned_clip, get_owned_video
from app.clips.base import TIME_TOLERANCE_SECONDS
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.database import get_db
from app.models.clip import (
    ALLOWED_CLIP_STATUS_TRANSITIONS,
    Clip,
    ClipSource,
    ClipStatus,
    is_allowed_clip_status_change,
)
from app.models.video import Video, VideoStatus
from app.repositories.clips import list_clips_for_video
from app.repositories.transcripts import get_transcript_for_video
from app.schemas.clip import (
    ClipCreate,
    ClipDetectionQueued,
    ClipList,
    ClipRead,
    ClipUpdate,
)
from app.worker.clip_detection import (
    get_active_clip_detection_job,
    queue_clip_detection,
)
from app.worker.jobs import JobDispatchError

logger = logging.getLogger(__name__)

MANUAL_CLIP_SCORE = 1.0
MANUAL_CLIP_REASON = "Created manually."

router = APIRouter(tags=["clips"])


def _log(video: Video, **extra: object) -> dict[str, object]:
    return {"user_id": str(video.user_id), "video_id": str(video.id), **extra}


def _format_time(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _require_duration(video: Video) -> float:
    duration = video.duration_seconds
    if video.status is not VideoStatus.READY or duration is None or duration <= 0:
        raise AppError(
            status.HTTP_409_CONFLICT,
            "video_not_ready",
            "The video must be ready before its clips can be changed.",
        )
    return duration


def _check_range(start: float, end: float, duration: float, settings: Settings) -> None:
    """Raise 422 unless 0 <= start < end <= duration and the length is allowed."""
    if not 0 <= start < end <= duration:
        raise AppError(
            422,
            "invalid_clip_bounds",
            "The clip must start at or after 0, end after it starts, and end "
            "within the video.",
            {"duration_seconds": duration},
        )
    length = end - start
    if (
        length < settings.CLIP_MIN_SECONDS - TIME_TOLERANCE_SECONDS
        or length > settings.CLIP_MAX_SECONDS + TIME_TOLERANCE_SECONDS
    ):
        raise AppError(
            422,
            "invalid_clip_length",
            f"A clip must be between {settings.CLIP_MIN_SECONDS:g} and "
            f"{settings.CLIP_MAX_SECONDS:g} seconds long.",
            {
                "min_seconds": settings.CLIP_MIN_SECONDS,
                "max_seconds": settings.CLIP_MAX_SECONDS,
            },
        )


def _commit(db: Session, event: str, log_extra: dict[str, object]) -> None:
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        # Only the type: database errors carry bound parameters.
        logger.error(event, extra={**log_extra, "error_type": type(exc).__name__})
        raise AppError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "clip_update_failed",
            "The clip could not be saved.",
        ) from None


# Detection


@router.post(
    "/videos/{video_id}/clips/detect",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ClipDetectionQueued,
    responses={
        409: {
            "description": "The video is not ready, has no transcript, or clip "
            "detection is already running."
        },
        503: {"description": "The job could not be queued."},
    },
)
def start_clip_detection(
    video: Video = Depends(get_owned_video),
    db: Session = Depends(get_db),
) -> ClipDetectionQueued:
    """Queue clip detection for the video's transcript."""
    log_extra = _log(video)
    if video.status is not VideoStatus.READY or not video.duration_seconds:
        logger.info(
            "clip_detection_rejected", extra={**log_extra, "reason": "not_ready"}
        )
        raise AppError(
            status.HTTP_409_CONFLICT,
            "video_not_ready",
            "The video must be ready before clips can be detected.",
        )
    if get_transcript_for_video(db, video.id) is None:
        logger.info(
            "clip_detection_rejected", extra={**log_extra, "reason": "no_transcript"}
        )
        raise AppError(
            status.HTTP_409_CONFLICT,
            "transcript_required",
            "The video has no transcript. Transcribe it first.",
        )
    if get_active_clip_detection_job(db, video.id) is not None:
        logger.info(
            "clip_detection_rejected", extra={**log_extra, "reason": "already_running"}
        )
        raise AppError(
            status.HTTP_409_CONFLICT,
            "clip_detection_in_progress",
            "Clips are already being detected for this video.",
        )

    try:
        job = queue_clip_detection(db, video)
    except JobDispatchError:
        raise AppError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "queue_unavailable",
            "Clip detection could not be started. Try again later.",
        ) from None
    except Exception as exc:
        db.rollback()
        logger.error(
            "clip_detection_queue_failed",
            extra={**log_extra, "error_type": type(exc).__name__},
        )
        raise AppError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "clip_detection_failed",
            "Clip detection could not be started.",
        ) from None

    logger.info("clip_detection_requested", extra={**log_extra, "job_id": str(job.id)})
    return ClipDetectionQueued(job_id=job.id, status=job.status)


# Listing and manual clips


@router.get("/videos/{video_id}/clips", response_model=ClipList)
def list_clips(
    status_filter: ClipStatus | None = Query(default=None, alias="status"),
    video: Video = Depends(get_owned_video),
    db: Session = Depends(get_db),
) -> ClipList:
    clips = list_clips_for_video(db, video.id, status_filter)
    return ClipList(
        items=[ClipRead.model_validate(clip) for clip in clips], total=len(clips)
    )


@router.post(
    "/videos/{video_id}/clips",
    status_code=status.HTTP_201_CREATED,
    response_model=ClipRead,
    responses={
        409: {"description": "The video is not ready."},
        422: {"description": "Invalid clip range, length or title."},
    },
)
def create_clip(
    body: ClipCreate,
    video: Video = Depends(get_owned_video),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ClipRead:
    """Create a manual clip; it is accepted and kept by re-detection."""
    duration = _require_duration(video)
    _check_range(body.start_seconds, body.end_seconds, duration, settings)
    clip = Clip(
        video_id=video.id,
        start_seconds=body.start_seconds,
        end_seconds=body.end_seconds,
        title=body.title or f"Clip at {_format_time(body.start_seconds)}",
        reason=MANUAL_CLIP_REASON,
        score=MANUAL_CLIP_SCORE,
        status=ClipStatus.ACCEPTED,
        source=ClipSource.MANUAL,
    )
    db.add(clip)
    log_extra = _log(video)
    _commit(db, "clip_create_failed", log_extra)
    db.refresh(clip)
    logger.info("clip_created", extra={**log_extra, "clip_id": str(clip.id)})
    return ClipRead.model_validate(clip)


# Single clips


def _lock_clip(db: Session, clip: Clip) -> Clip:
    """Reload the clip with its video row locked.

    The lock serializes the change with a detection job replacing the
    video's suggestions (PostgreSQL); a clip replaced meanwhile is 404.
    """
    db.execute(select(Video.id).where(Video.id == clip.video_id).with_for_update())
    locked = db.scalars(
        select(Clip).where(Clip.id == clip.id).execution_options(populate_existing=True)
    ).one_or_none()
    if locked is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Clip not found.")
    return locked


@router.patch(
    "/clips/{clip_id}",
    response_model=ClipRead,
    responses={
        404: {"description": "Unknown clip, or another user's clip."},
        409: {"description": "Status change not allowed, or the video is not ready."},
        422: {"description": "Invalid clip range, length, title or status."},
    },
)
def update_clip(
    body: ClipUpdate,
    clip: Clip = Depends(get_owned_clip),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ClipRead:
    """Change a clip's range, title and/or status."""
    clip = _lock_clip(db, clip)
    video = clip.video
    log_extra = _log(video, clip_id=str(clip.id))
    fields = body.model_fields_set

    # Everything is checked before anything is changed.
    start, end = clip.start_seconds, clip.end_seconds
    if "start_seconds" in fields or "end_seconds" in fields:
        duration = _require_duration(video)
        if body.start_seconds is not None:
            start = body.start_seconds
        if body.end_seconds is not None:
            end = body.end_seconds
        _check_range(start, end, duration, settings)
    if body.status is not None and not is_allowed_clip_status_change(
        clip.status, body.status
    ):
        logger.info(
            "clip_status_change_rejected",
            extra={
                **log_extra,
                "from_status": clip.status.value,
                "to_status": body.status.value,
            },
        )
        raise AppError(
            status.HTTP_409_CONFLICT,
            "invalid_status_transition",
            f"A {clip.status.value} clip cannot be changed to {body.status.value}.",
            {
                "allowed": sorted(
                    s.value for s in ALLOWED_CLIP_STATUS_TRANSITIONS[clip.status]
                )
            },
        )

    clip.start_seconds, clip.end_seconds = start, end
    if body.title is not None:
        clip.title = body.title
    if body.status is not None and body.status is not clip.status:
        logger.info(
            "clip_status_changed",
            extra={
                **log_extra,
                "from_status": clip.status.value,
                "to_status": body.status.value,
            },
        )
        clip.status = body.status

    _commit(db, "clip_update_failed", log_extra)
    db.refresh(clip)
    logger.info("clip_updated", extra={**log_extra, "fields": sorted(fields)})
    return ClipRead.model_validate(clip)


@router.delete(
    "/clips/{clip_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={404: {"description": "Unknown clip, or another user's clip."}},
)
def delete_clip(
    clip: Clip = Depends(get_owned_clip),
    db: Session = Depends(get_db),
) -> Response:
    log_extra = _log(clip.video, clip_id=str(clip.id))
    db.delete(clip)
    _commit(db, "clip_delete_failed", log_extra)
    logger.info("clip_deleted", extra=log_extra)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
