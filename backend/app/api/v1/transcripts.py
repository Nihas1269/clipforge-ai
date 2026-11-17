"""Transcription jobs, transcripts, transcript search and subtitle downloads.

Every endpoint requires authentication and works only on the current user's
videos (get_owned_video: another user's video gets the same 404 as a
missing one).

`POST /videos/{id}/transcription` queues a `transcription` job (see
app.worker.transcription) and returns 202 with its id; follow it with
`GET /jobs/{job_id}`. The video must be `ready` and must not already have a
queued or running transcription job (409 otherwise). The previous
transcript stays available until the new one has been stored.

Timestamps are always seconds of source-video time. Subtitle downloads are
generated from the stored segments with app.subtitles.formats; they are
never cached by shared caches.
"""

import logging

from fastapi import APIRouter, Body, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_owned_video
from app.core.errors import AppError
from app.database import get_db
from app.models.transcript import Transcript
from app.models.video import Video, VideoStatus
from app.repositories.transcripts import get_transcript_for_video
from app.schemas.transcript import (
    TranscriptionQueued,
    TranscriptionRequest,
    TranscriptRead,
    TranscriptSearchResult,
    TranscriptSegmentRead,
)
from app.subtitles.formats import (
    SRT_MEDIA_TYPE,
    VTT_MEDIA_TYPE,
    cues_from_segments,
    subtitle_filename,
    to_srt,
    to_vtt,
)
from app.worker.jobs import JobDispatchError
from app.worker.transcription import get_active_transcription_job, queue_transcription

logger = logging.getLogger(__name__)

SEARCH_QUERY_MAX_LENGTH = 200
DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 500
# Transcripts are private to the owner and must not be stored by shared caches.
TRANSCRIPT_CACHE_CONTROL = "private, no-store"

router = APIRouter(prefix="/videos", tags=["transcripts"])


def _log(video: Video, **extra: object) -> dict[str, object]:
    return {"user_id": str(video.user_id), "video_id": str(video.id), **extra}


def _require_transcript(db: Session, video: Video) -> Transcript:
    transcript = get_transcript_for_video(db, video.id)
    if transcript is None:
        raise AppError(
            status.HTTP_404_NOT_FOUND,
            "transcript_not_found",
            "This video has no transcript.",
        )
    return transcript


def _transcript_read(transcript: Transcript) -> TranscriptRead:
    segments = [TranscriptSegmentRead.model_validate(s) for s in transcript.segments]
    word_level = bool(segments) and all(s.words is not None for s in segments)
    return TranscriptRead(
        id=transcript.id,
        video_id=transcript.video_id,
        language=transcript.language,
        requested_language=transcript.requested_language,
        provider=transcript.provider,
        model=transcript.model,
        full_text=transcript.full_text,
        timing_granularity="word" if word_level else "segment",
        created_at=transcript.created_at,
        segments=segments,
    )


@router.post(
    "/{video_id}/transcription",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TranscriptionQueued,
    responses={
        409: {
            "description": "The video is not ready, or is already being transcribed."
        },
        503: {"description": "The job could not be queued."},
    },
)
def start_transcription(
    body: TranscriptionRequest | None = Body(default=None),
    video: Video = Depends(get_owned_video),
    db: Session = Depends(get_db),
) -> TranscriptionQueued:
    """Queue transcription of the video, optionally in a given language."""
    language = body.language if body is not None else None
    log_extra = _log(video, requested_language=language)
    if video.status is not VideoStatus.READY:
        logger.info(
            "transcription_rejected", extra={**log_extra, "reason": "not_ready"}
        )
        raise AppError(
            status.HTTP_409_CONFLICT,
            "video_not_ready",
            "The video must be ready before it can be transcribed.",
        )
    if get_active_transcription_job(db, video.id) is not None:
        logger.info(
            "transcription_rejected", extra={**log_extra, "reason": "already_running"}
        )
        raise AppError(
            status.HTTP_409_CONFLICT,
            "transcription_in_progress",
            "This video is already being transcribed.",
        )

    try:
        job = queue_transcription(db, video, language)
    except JobDispatchError:
        raise AppError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "queue_unavailable",
            "Transcription could not be started. Try again later.",
        ) from None
    except Exception as exc:
        db.rollback()
        # Only the type: database errors carry bound parameters.
        logger.error(
            "transcription_queue_failed",
            extra={**log_extra, "error_type": type(exc).__name__},
        )
        raise AppError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "transcription_failed",
            "Transcription could not be started.",
        ) from None

    logger.info("transcription_requested", extra={**log_extra, "job_id": str(job.id)})
    return TranscriptionQueued(job_id=job.id, status=job.status)


@router.get(
    "/{video_id}/transcript",
    response_model=TranscriptRead,
    responses={404: {"description": "Unknown video, or the video has no transcript."}},
)
def read_transcript(
    video: Video = Depends(get_owned_video),
    db: Session = Depends(get_db),
) -> TranscriptRead:
    return _transcript_read(_require_transcript(db, video))


@router.get(
    "/{video_id}/transcript/search",
    response_model=TranscriptSearchResult,
    responses={404: {"description": "Unknown video, or the video has no transcript."}},
)
def search_transcript(
    q: str = Query(min_length=1, max_length=SEARCH_QUERY_MAX_LENGTH),
    limit: int = Query(DEFAULT_SEARCH_LIMIT, ge=1, le=MAX_SEARCH_LIMIT),
    video: Video = Depends(get_owned_video),
    db: Session = Depends(get_db),
) -> TranscriptSearchResult:
    """Return the segments whose text contains `q` (case-insensitive).

    Whitespace in the query and the text is collapsed before comparing.
    `total` counts every match; at most `limit` are returned.
    """
    query = " ".join(q.split())
    if not query:
        raise AppError(422, "invalid_query", "The search query must not be blank.")
    needle = query.casefold()
    transcript = _require_transcript(db, video)
    matches = [
        segment
        for segment in transcript.segments
        if needle in " ".join(segment.text.split()).casefold()
    ]
    return TranscriptSearchResult(
        query=query,
        total=len(matches),
        items=[TranscriptSegmentRead.model_validate(s) for s in matches[:limit]],
    )


def _subtitle_response(
    video: Video, content: str, extension: str, media_type: str
) -> Response:
    filename = subtitle_filename(video.original_filename, extension)
    logger.info(
        "transcript_subtitles_downloaded", extra=_log(video, subtitle_format=extension)
    )
    return Response(
        content=content.encode("utf-8"),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": TRANSCRIPT_CACHE_CONTROL,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/{video_id}/transcript.srt",
    response_class=Response,
    responses={
        200: {
            "content": {"application/x-subrip": {}},
            "description": "SubRip subtitles.",
        },
        404: {"description": "Unknown video, or the video has no transcript."},
    },
)
def download_srt(
    video: Video = Depends(get_owned_video),
    db: Session = Depends(get_db),
) -> Response:
    transcript = _require_transcript(db, video)
    content = to_srt(cues_from_segments(transcript.segments))
    return _subtitle_response(video, content, "srt", SRT_MEDIA_TYPE)


@router.get(
    "/{video_id}/transcript.vtt",
    response_class=Response,
    responses={
        200: {"content": {"text/vtt": {}}, "description": "WebVTT subtitles."},
        404: {"description": "Unknown video, or the video has no transcript."},
    },
)
def download_vtt(
    video: Video = Depends(get_owned_video),
    db: Session = Depends(get_db),
) -> Response:
    transcript = _require_transcript(db, video)
    content = to_vtt(cues_from_segments(transcript.segments))
    return _subtitle_response(video, content, "vtt", VTT_MEDIA_TYPE)
