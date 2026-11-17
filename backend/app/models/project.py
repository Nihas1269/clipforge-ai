import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

PROJECT_NAME_MAX_LENGTH = 100


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    """A user's folder for videos. Projects are archived, never deleted.

    A project belongs to one user; it is only ever visible to that user.
    Videos are linked through `project_videos`; a video without a link is
    "unassigned".
    """

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(PROJECT_NAME_MAX_LENGTH))
    # Set while the project is archived; archived projects accept no videos.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    # Also bumped when a video is moved into the project ("recent projects").
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    @property
    def archived(self) -> bool:
        return self.archived_at is not None

    def __repr__(self) -> str:
        # No name: it is chosen by the user and can contain personal data.
        return f"Project(id={self.id!r})"


class ProjectVideo(Base):
    """Links a video to the project it is filed under (at most one)."""

    __tablename__ = "project_videos"

    # The primary key keeps a video in at most one project.
    video_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"ProjectVideo(video_id={self.video_id!r}, project_id={self.project_id!r})"
        )
