import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.transcription.base import LANGUAGE_MAX_LENGTH

if TYPE_CHECKING:
    from app.models.video import Video

PROVIDER_MAX_LENGTH = 50
MODEL_MAX_LENGTH = 100


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Transcript(Base):
    """The transcript of a video (at most one per video).

    `language` is the language the provider reported, as an ISO 639 code (or
    "und"); NULL means unknown. `requested_language` is the language the user
    asked for, NULL for automatic detection. Both are stored as given and
    never relabeled.
    """

    __tablename__ = "transcripts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Unique: a video has at most one transcript; it belongs to the video's owner.
    video_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("videos.id", ondelete="CASCADE"), unique=True, index=True
    )
    language: Mapped[str | None] = mapped_column(String(LANGUAGE_MAX_LENGTH))
    requested_language: Mapped[str | None] = mapped_column(String(LANGUAGE_MAX_LENGTH))
    provider: Mapped[str] = mapped_column(String(PROVIDER_MAX_LENGTH))
    model: Mapped[str | None] = mapped_column(String(MODEL_MAX_LENGTH))
    full_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )

    video: Mapped["Video"] = relationship(back_populates="transcript")
    # The database deletes the segments with the transcript (ON DELETE CASCADE).
    segments: Mapped[list["TranscriptSegment"]] = relationship(
        back_populates="transcript",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TranscriptSegment.index",
    )

    def __repr__(self) -> str:
        # No text: it is the user's content.
        return f"Transcript(id={self.id!r}, language={self.language!r})"


class TranscriptSegment(Base):
    """A timed span of a transcript, in seconds from the start of the audio."""

    __tablename__ = "transcript_segments"
    __table_args__ = (
        # Also the index used to load a transcript's segments in order.
        UniqueConstraint(
            "transcript_id", "index", name="uq_transcript_segments_transcript_id_index"
        ),
        CheckConstraint('"index" >= 0', name="ck_transcript_segments_index_non_negative"),
        CheckConstraint(
            "start_seconds >= 0", name="ck_transcript_segments_start_non_negative"
        ),
        CheckConstraint(
            "end_seconds > start_seconds", name="ck_transcript_segments_end_after_start"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    transcript_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("transcripts.id", ondelete="CASCADE")
    )
    index: Mapped[int] = mapped_column(Integer)
    start_seconds: Mapped[float] = mapped_column(Float)
    end_seconds: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text)
    # [{"word": str, "start": float, "end": float}, ...]; NULL (not JSON
    # "null") when the provider reported no word timings.
    words: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON(none_as_null=True))

    transcript: Mapped[Transcript] = relationship(back_populates="segments")

    def __repr__(self) -> str:
        return f"TranscriptSegment(id={self.id!r}, index={self.index!r})"
