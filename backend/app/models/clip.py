import enum
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.video import Video

TITLE_MAX_LENGTH = 200


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ClipStatus(str, enum.Enum):
    """Review state of a clip suggestion."""

    SUGGESTED = "suggested"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ClipSource(str, enum.Enum):
    """What produced a clip."""

    HEURISTIC = "heuristic"
    LLM = "llm"
    MANUAL = "manual"


def _enum_values(members: type[enum.Enum]) -> list[str]:
    return [member.value for member in members]


class Clip(Base):
    """A suggested (or user-defined) clip of a video, in source-video seconds.

    A clip belongs to its video's owner; ownership is always checked through
    the video. `score` is the detector's confidence in [0, 1] (see
    app.clips.heuristic for what the heuristic score means).
    """

    __tablename__ = "clips"
    __table_args__ = (
        # Loads a video's clips in timeline order; also serves the foreign key.
        Index("ix_clips_video_id_start_seconds", "video_id", "start_seconds"),
        CheckConstraint("start_seconds >= 0", name="ck_clips_start_non_negative"),
        CheckConstraint("end_seconds > start_seconds", name="ck_clips_end_after_start"),
        CheckConstraint("score >= 0 AND score <= 1", name="ck_clips_score_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    video_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("videos.id", ondelete="CASCADE")
    )
    start_seconds: Mapped[float] = mapped_column(Float)
    end_seconds: Mapped[float] = mapped_column(Float)
    title: Mapped[str] = mapped_column(String(TITLE_MAX_LENGTH))
    reason: Mapped[str] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float)
    status: Mapped[ClipStatus] = mapped_column(
        Enum(
            ClipStatus,
            name="clip_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=_enum_values,
        ),
        default=ClipStatus.SUGGESTED,
        server_default=ClipStatus.SUGGESTED.value,
    )
    source: Mapped[ClipSource] = mapped_column(
        Enum(
            ClipSource,
            name="clip_source",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=_enum_values,
        )
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    video: Mapped["Video"] = relationship(back_populates="clips")

    def __repr__(self) -> str:
        # No title or reason: they are derived from the user's content.
        status = (
            self.status.value if isinstance(self.status, ClipStatus) else self.status
        )
        return f"Clip(id={self.id!r}, status={status!r})"
