import enum
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.clip import Clip
    from app.models.project import Project
    from app.models.transcript import Transcript
    from app.models.user import User

logger = logging.getLogger(__name__)

FILENAME_MAX_LENGTH = 255
CONTENT_TYPE_MAX_LENGTH = 255
STORAGE_KEY_MAX_LENGTH = 1024

DEFAULT_FAILURE_MESSAGE = "Video processing failed."


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class VideoStatus(str, enum.Enum):
    """Processing lifecycle of an uploaded video."""

    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


# The single source of truth for status changes; use transition_status().
ALLOWED_TRANSITIONS: dict[VideoStatus, frozenset[VideoStatus]] = {
    VideoStatus.UPLOADED: frozenset({VideoStatus.PROCESSING}),
    VideoStatus.PROCESSING: frozenset({VideoStatus.READY, VideoStatus.FAILED}),
    # Retry after a failure.
    VideoStatus.FAILED: frozenset({VideoStatus.PROCESSING}),
    # Reprocess a finished video.
    VideoStatus.READY: frozenset({VideoStatus.PROCESSING}),
}


class InvalidStatusTransitionError(Exception):
    """Raised when a video status change is not allowed by the lifecycle."""

    def __init__(self, current: object, requested: object) -> None:
        self.current = current
        self.requested = requested
        super().__init__(
            f"Cannot change video status from {_status_label(current)!r} "
            f"to {_status_label(requested)!r}."
        )


def _status_label(status: object) -> str:
    return status.value if isinstance(status, VideoStatus) else str(status)


class Video(Base):
    """An uploaded long-form video and its processing state."""

    __tablename__ = "videos"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="ck_videos_size_bytes_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    original_filename: Mapped[str] = mapped_column(String(FILENAME_MAX_LENGTH))
    content_type: Mapped[str] = mapped_column(String(CONTENT_TYPE_MAX_LENGTH))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(String(STORAGE_KEY_MAX_LENGTH))
    status: Mapped[VideoStatus] = mapped_column(
        Enum(
            VideoStatus,
            name="video_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        default=VideoStatus.UPLOADED,
        server_default=VideoStatus.UPLOADED.value,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    # Media metadata, filled in once the video has been probed.
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    user: Mapped["User"] = relationship(back_populates="videos")
    # The database deletes the transcript with the video (ON DELETE CASCADE).
    transcript: Mapped["Transcript | None"] = relationship(
        back_populates="video",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    # The database deletes the clips with the video (ON DELETE CASCADE).
    clips: Mapped[list["Clip"]] = relationship(
        back_populates="video",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Clip.start_seconds",
    )
    # Read-only: change it with app.repositories.projects.set_video_project.
    # None means the video is unassigned.
    project: Mapped["Project | None"] = relationship(
        secondary="project_videos", uselist=False, viewonly=True
    )

    def __repr__(self) -> str:
        # No filename or storage key: they can contain personal data.
        return f"Video(id={self.id!r}, status={_status_label(self.status)!r})"


def transition_status(
    video: Video, new_status: VideoStatus | str, error: str | None = None
) -> Video:
    """Move `video` to `new_status` if the lifecycle allows it.

    Allowed: uploaded -> processing, processing -> ready, processing -> failed,
    failed -> processing (retry) and ready -> processing (reprocess). Any other
    change, including staying in the same status, raises
    InvalidStatusTransitionError and leaves the video unchanged.

    `error` is stored as `error_message` when moving to `failed` (a generic
    message is used when it is missing or blank); every other transition
    clears `error_message`, and passing `error` to it raises ValueError.

    The change is not committed; the caller owns the session.
    """
    current = video.status
    try:
        target = VideoStatus(new_status)
        source = VideoStatus(current)
    except ValueError:
        raise InvalidStatusTransitionError(current, new_status) from None

    if target not in ALLOWED_TRANSITIONS[source]:
        raise InvalidStatusTransitionError(source, target)
    if error is not None and target is not VideoStatus.FAILED:
        raise ValueError("An error message can only be set when a video fails.")

    if target is VideoStatus.FAILED:
        message = error.strip() if error is not None else ""
        video.error_message = message or DEFAULT_FAILURE_MESSAGE
    else:
        video.error_message = None
    video.status = target

    logger.info(
        "video_status_changed",
        extra={
            "video_id": str(video.id),
            "from_status": source.value,
            "to_status": target.value,
        },
    )
    return video
