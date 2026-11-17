"""Loading, listing and replacing a video's clips.

Ownership is always checked through the parent video: a clip is only found
together with a video that belongs to the given user.
"""

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.clips.base import TIME_TOLERANCE_SECONDS, ClipCandidate
from app.models.clip import Clip, ClipSource, ClipStatus
from app.models.video import Video

logger = logging.getLogger(__name__)


def get_clip_for_user(
    db: Session, clip_id: uuid.UUID, user_id: uuid.UUID
) -> Clip | None:
    """Return the clip if its video belongs to `user_id`, otherwise None."""
    return db.scalars(
        select(Clip)
        .join(Video, Video.id == Clip.video_id)
        .where(Clip.id == clip_id, Video.user_id == user_id)
    ).one_or_none()


def list_clips_for_video(
    db: Session, video_id: uuid.UUID, status: ClipStatus | None = None
) -> list[Clip]:
    """Return the video's clips, best score first, then earlier start.

    Ties are broken by the earlier end and then the id, so the order is
    always the same.
    """
    statement = select(Clip).where(Clip.video_id == video_id)
    if status is not None:
        statement = statement.where(Clip.status == status)
    statement = statement.order_by(
        Clip.score.desc(), Clip.start_seconds, Clip.end_seconds, Clip.id
    )
    return list(db.scalars(statement))


def _same_range(clip: Clip, candidate: ClipCandidate) -> bool:
    return (
        abs(clip.start_seconds - candidate.start) <= TIME_TOLERANCE_SECONDS
        and abs(clip.end_seconds - candidate.end) <= TIME_TOLERANCE_SECONDS
    )


def _overlaps(clip: Clip, candidate: ClipCandidate) -> bool:
    return candidate.start < clip.end_seconds and clip.start_seconds < candidate.end


def replace_suggestions(
    db: Session,
    video_id: uuid.UUID,
    candidates: Sequence[ClipCandidate],
    *,
    source: ClipSource,
    detector_version: str | None = None,
    detector_model: str | None = None,
    prompt_version: str | None = None,
) -> list[Clip]:
    """Replace the video's suggested clips with `candidates`; return the new ones.

    Only detected clips that are still `suggested` are deleted. Accepted,
    rejected and manual clips are kept unchanged. A candidate is skipped when
    it overlaps a kept accepted or manual clip (that moment is already
    chosen), or when it has the same range as a rejected clip (the user
    already turned it down), so re-detection never duplicates a decision.

    The change is flushed but not committed; the caller owns the transaction.
    """
    if source is ClipSource.MANUAL:
        raise ValueError("Detected clips cannot have the manual source.")
    deleted = db.execute(
        delete(Clip)
        .where(
            Clip.video_id == video_id,
            Clip.status == ClipStatus.SUGGESTED,
            Clip.source != ClipSource.MANUAL,
        )
        .execution_options(synchronize_session="fetch")
    )
    kept = list(db.scalars(select(Clip).where(Clip.video_id == video_id)))
    chosen = [
        clip
        for clip in kept
        if clip.status is ClipStatus.ACCEPTED or clip.source is ClipSource.MANUAL
    ]
    rejected = [clip for clip in kept if clip.status is ClipStatus.REJECTED]

    created: list[Clip] = []
    skipped = 0
    for candidate in candidates:
        if any(_overlaps(clip, candidate) for clip in chosen) or any(
            _same_range(clip, candidate) for clip in rejected
        ):
            skipped += 1
            continue
        clip = Clip(
            video_id=video_id,
            start_seconds=candidate.start,
            end_seconds=candidate.end,
            title=candidate.title,
            reason=candidate.reason,
            score=candidate.score,
            status=ClipStatus.SUGGESTED,
            source=source,
            detector_version=detector_version,
            detector_model=detector_model,
            prompt_version=prompt_version,
        )
        db.add(clip)
        created.append(clip)
    db.flush()
    logger.info(
        "clip_suggestions_replaced",
        extra={
            "video_id": str(video_id),
            "source": source.value,
            "detector_version": detector_version,
            # "created" is a reserved LogRecord attribute, so the keys carry
            # a suffix.
            "deleted_count": getattr(deleted, "rowcount", None),
            "kept_count": len(kept),
            "created_count": len(created),
            "skipped_count": skipped,
        },
    )
    return created
