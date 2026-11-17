import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.clip import Clip
from app.models.project import ProjectVideo
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
    project_id: uuid.UUID | None = None,
    unassigned: bool = False,
) -> tuple[list[Video], int]:
    """Return one page of the user's videos, newest first, and the total count.

    With `status`, only videos in that status are listed and counted; with
    `project_id`, only the videos in that project; with `unassigned`, only
    videos in no project. The caller checks that the project is the user's.
    """
    conditions: list[Any] = [Video.user_id == user_id]
    if status is not None:
        conditions.append(Video.status == status)
    if project_id is not None:
        conditions.append(
            Video.id.in_(
                select(ProjectVideo.video_id).where(
                    ProjectVideo.project_id == project_id
                )
            )
        )
    if unassigned:
        conditions.append(Video.id.not_in(select(ProjectVideo.video_id)))
    total = db.scalar(select(func.count()).select_from(Video).where(*conditions))
    videos = db.scalars(
        select(Video)
        .where(*conditions)
        .options(selectinload(Video.project))
        # The id breaks ties between videos created at the same instant, so
        # pages never overlap or skip a video.
        .order_by(Video.created_at.desc(), Video.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return list(videos), total or 0


def video_totals_for_user(db: Session, user_id: uuid.UUID) -> dict[str, Any]:
    """Return the user's video counts by status, total size and duration."""
    rows = db.execute(
        select(
            Video.status,
            func.count(),
            func.coalesce(func.sum(Video.size_bytes), 0),
            func.coalesce(func.sum(Video.duration_seconds), 0.0),
        )
        .where(Video.user_id == user_id)
        .group_by(Video.status)
    ).all()
    by_status = {status.value: 0 for status in VideoStatus}
    size_bytes = 0
    duration_seconds = 0.0
    for status, count, size, duration in rows:
        by_status[VideoStatus(status).value] = count
        size_bytes += int(size)
        duration_seconds += float(duration)
    return {
        "by_status": by_status,
        "total": sum(by_status.values()),
        "size_bytes": size_bytes,
        "duration_seconds": duration_seconds,
    }


def count_clips_for_user(db: Session, user_id: uuid.UUID) -> int:
    """Return the number of clips of the user's videos."""
    total = db.scalar(
        select(func.count())
        .select_from(Clip)
        .join(Video, Video.id == Clip.video_id)
        .where(Video.user_id == user_id)
    )
    return total or 0
