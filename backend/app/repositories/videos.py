import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.video import Video, VideoStatus


def get_video_for_user(
    db: Session, video_id: uuid.UUID, user_id: uuid.UUID
) -> Video | None:
    """Return the video if it exists and belongs to `user_id`, else None."""
    return db.scalars(
        select(Video).where(Video.id == video_id, Video.user_id == user_id)
    ).one_or_none()


def list_videos_for_user(
    db: Session,
    user_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
    status: VideoStatus | None = None,
) -> tuple[list[Video], int]:
    """Return one page of the user's videos, newest first, and the total count.

    With `status`, only videos in that status are listed and counted.
    """
    conditions = [Video.user_id == user_id]
    if status is not None:
        conditions.append(Video.status == status)
    total = db.scalar(select(func.count()).select_from(Video).where(*conditions))
    videos = db.scalars(
        select(Video)
        .where(*conditions)
        # The id breaks ties between videos created at the same instant, so
        # pages never overlap or skip a video.
        .order_by(Video.created_at.desc(), Video.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return list(videos), total or 0
