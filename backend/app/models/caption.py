import enum
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
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

from app.captions.styles import (
    COLOR_MAX_LENGTH,
    FONT_FAMILY_MAX_LENGTH,
    FONT_SIZE_MAX,
    FONT_SIZE_MIN,
    MAX_CHARS_PER_LINE_MAX,
    MAX_CHARS_PER_LINE_MIN,
    CaptionPosition,
    CaptionPreset,
)
from app.database import Base

if TYPE_CHECKING:
    from app.models.clip import Clip


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _enum_values(members: type[enum.Enum]) -> list[str]:
    return [member.value for member in members]


class ClipCaption(Base):
    """One caption of a clip, in seconds relative to the clip start.

    Ownership is checked through the clip's video. Captions are ordered by
    `index` and do not overlap (checked by the API).
    """

    __tablename__ = "clip_captions"
    __table_args__ = (
        # Also the index used to load a clip's captions in order.
        UniqueConstraint("clip_id", "index", name="uq_clip_captions_clip_id_index"),
        CheckConstraint('"index" >= 0', name="ck_clip_captions_index_non_negative"),
        CheckConstraint(
            "start_seconds >= 0", name="ck_clip_captions_start_non_negative"
        ),
        CheckConstraint(
            "end_seconds > start_seconds", name="ck_clip_captions_end_after_start"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    clip_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("clips.id", ondelete="CASCADE")
    )
    index: Mapped[int] = mapped_column(Integer)
    start_seconds: Mapped[float] = mapped_column(Float)
    end_seconds: Mapped[float] = mapped_column(Float)
    # Lines separated by "\n".
    text: Mapped[str] = mapped_column(Text)

    clip: Mapped["Clip"] = relationship(back_populates="captions")

    def __repr__(self) -> str:
        # No text: it is the user's content.
        return f"ClipCaption(id={self.id!r}, index={self.index!r})"


class CaptionStyle(Base):
    """The caption style of a clip (at most one; see app.captions.styles)."""

    __tablename__ = "caption_styles"
    __table_args__ = (
        CheckConstraint(
            f"font_size >= {FONT_SIZE_MIN} AND font_size <= {FONT_SIZE_MAX}",
            name="ck_caption_styles_font_size_range",
        ),
        CheckConstraint(
            f"max_chars_per_line >= {MAX_CHARS_PER_LINE_MIN} "
            f"AND max_chars_per_line <= {MAX_CHARS_PER_LINE_MAX}",
            name="ck_caption_styles_max_chars_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    clip_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("clips.id", ondelete="CASCADE"), unique=True, index=True
    )
    preset: Mapped[CaptionPreset] = mapped_column(
        Enum(
            CaptionPreset,
            name="caption_preset",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=_enum_values,
        )
    )
    font_family: Mapped[str] = mapped_column(String(FONT_FAMILY_MAX_LENGTH))
    font_size: Mapped[int] = mapped_column(Integer)
    # "#RRGGBBAA", upper case.
    primary_color: Mapped[str] = mapped_column(String(COLOR_MAX_LENGTH))
    outline_color: Mapped[str] = mapped_column(String(COLOR_MAX_LENGTH))
    position: Mapped[CaptionPosition] = mapped_column(
        Enum(
            CaptionPosition,
            name="caption_position",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=_enum_values,
        )
    )
    uppercase: Mapped[bool] = mapped_column(Boolean)
    max_chars_per_line: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    clip: Mapped["Clip"] = relationship(back_populates="caption_style")

    def __repr__(self) -> str:
        preset = (
            self.preset.value if isinstance(self.preset, CaptionPreset) else self.preset
        )
        return f"CaptionStyle(id={self.id!r}, preset={preset!r})"
