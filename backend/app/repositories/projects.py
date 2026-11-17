"""Projects and the assignment of videos to them.

Every function takes the owner's id and only ever reads or changes that
user's rows; ownership is checked here, not by the database.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.project import Project, ProjectVideo
from app.models.video import Video, VideoStatus

logger = logging.getLogger(__name__)


class ProjectArchivedError(Exception):
    """Videos cannot be moved into an archived project."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_project_for_user(
    db: Session, project_id: uuid.UUID, user_id: uuid.UUID
) -> Project | None:
    """Return the project if it exists and belongs to `user_id`, else None."""
    return db.scalars(
        select(Project).where(Project.id == project_id, Project.user_id == user_id)
    ).one_or_none()


def list_projects_for_user(
    db: Session,
    user_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
    archived: bool = False,
) -> tuple[list[Project], int]:
    """Return one page of the user's active (or archived) projects, most
    recently updated first, and the total count."""
    conditions = [
        Project.user_id == user_id,
        Project.archived_at.is_not(None) if archived else Project.archived_at.is_(None),
    ]
    total = db.scalar(select(func.count()).select_from(Project).where(*conditions))
    projects = db.scalars(
        select(Project)
        .where(*conditions)
        .order_by(Project.updated_at.desc(), Project.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return list(projects), total or 0


def count_projects_for_user(db: Session, user_id: uuid.UUID) -> tuple[int, int]:
    """Return the number of the user's active and archived projects."""
    rows = db.execute(
        select(Project.archived_at.is_not(None), func.count())
        .where(Project.user_id == user_id)
        .group_by(Project.archived_at.is_not(None))
    ).all()
    counts = {bool(archived): count for archived, count in rows}
    return counts.get(False, 0), counts.get(True, 0)


def status_counts_by_project(
    db: Session, user_id: uuid.UUID, project_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, int]]:
    """Return `{project_id: {status: count}}` for the user's videos in the
    given projects. Projects without videos are missing from the result."""
    if not project_ids:
        return {}
    rows = db.execute(
        select(ProjectVideo.project_id, Video.status, func.count())
        .join(Video, Video.id == ProjectVideo.video_id)
        .where(ProjectVideo.project_id.in_(project_ids), Video.user_id == user_id)
        .group_by(ProjectVideo.project_id, Video.status)
    ).all()
    result: dict[uuid.UUID, dict[str, int]] = {}
    for project_id, status, count in rows:
        result.setdefault(project_id, {})[VideoStatus(status).value] = count
    return result


def create_project(db: Session, user_id: uuid.UUID, name: str) -> Project:
    """Create and commit a new, active project."""
    project = Project(user_id=user_id, name=name)
    try:
        db.add(project)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    logger.info(
        "project_created",
        extra={"user_id": str(user_id), "project_id": str(project.id)},
    )
    return project


def update_project(
    db: Session,
    project: Project,
    *,
    name: str | None = None,
    archived: bool | None = None,
) -> Project:
    """Rename and/or archive or restore the project, and commit."""
    changes: list[str] = []
    if name is not None and name != project.name:
        project.name = name
        changes.append("renamed")
    if archived is not None and archived != project.archived:
        project.archived_at = _utcnow() if archived else None
        changes.append("archived" if archived else "restored")
    if changes:
        try:
            db.commit()
        except BaseException:
            db.rollback()
            raise
        logger.info(
            "project_updated",
            extra={
                "user_id": str(project.user_id),
                "project_id": str(project.id),
                "changes": changes,
            },
        )
    return project


def set_video_project(db: Session, video: Video, project: Project | None) -> Video:
    """Move `video` into `project`, or make it unassigned with None, and commit.

    The project must belong to the video's owner (the caller loads both
    scoped to the current user; this is checked again here) and must not be
    archived. Moving a video into its current project changes nothing.
    """
    if project is not None:
        if project.user_id != video.user_id:
            raise ValueError("The project belongs to another user.")
        if project.archived:
            raise ProjectArchivedError(str(project.id))

    current = db.scalar(
        select(ProjectVideo.project_id).where(ProjectVideo.video_id == video.id)
    )
    target = project.id if project is not None else None
    if current == target:
        return video

    try:
        db.execute(
            delete(ProjectVideo)
            .where(ProjectVideo.video_id == video.id)
            .execution_options(synchronize_session=False)
        )
        if project is not None:
            db.add(ProjectVideo(video_id=video.id, project_id=project.id))
            # Moving a video in makes the project "recent".
            project.updated_at = _utcnow()
        db.commit()
    except BaseException:
        db.rollback()
        raise
    # The viewonly relationship does not follow the link table by itself.
    db.expire(video, ["project"])
    logger.info(
        "video_project_changed",
        extra={
            "user_id": str(video.user_id),
            "video_id": str(video.id),
            "from_project_id": str(current) if current else None,
            "to_project_id": str(target) if target else None,
        },
    )
    return video
