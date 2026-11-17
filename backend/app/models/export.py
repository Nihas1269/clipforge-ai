"""Rendered exports of a clip (migration 0012).

An export is one request to render a clip as a finished short-form MP4:
trimmed to the clip, reframed to `aspect_ratio` (`reframe_mode` chooses a
center crop or face tracking) and, when `include_captions` is set, with the
clip's captions burned in. The `render` job (app.worker.render) moves it
through its statuses:

    queued -> rendering -> completed
                        -> failed

Status changes are conditional UPDATE statements in the worker, so a
duplicate or late job message can never move an export backwards. The
rendered file is stored at `users/{user_id}/exports/{id}.mp4`
(app.storage.keys.export_key); `storage_key` and `size_bytes` are set only
once the file has been validated and uploaded.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

STORAGE_KEY_MAX_LENGTH = 1024


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _enum_values(members: type[enum.Enum]) -> list[str]:
    return [member.value for member in members]


class ExportAspectRatio(str, enum.Enum):
    """Output aspect ratio (width:height)."""

    PORTRAIT = "9:16"
    SQUARE = "1:1"
    VERTICAL = "4:5"
    LANDSCAPE = "16:9"


class ReframeMode(str, enum.Enum):
    """How the crop window is placed (see app.reframe)."""

    CENTER = "center"
    FACE = "face"


class ExportStatus(str, enum.Enum):
    """Lifecycle of an export."""

    QUEUED = "queued"
    RENDERING = "rendering"
    COMPLETED = "completed"
    FAILED = "failed"


ACTIVE_EXPORT_STATUSES = frozenset({ExportStatus.QUEUED, ExportStatus.RENDERING})


class Export(Base):
    """A rendered (or to-be-rendered) short-form video of a clip."""

    __tablename__ = "exports"
    __table_args__ = (
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name="ck_exports_size_bytes_non_negative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    clip_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("clips.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    aspect_ratio: Mapped[ExportAspectRatio] = mapped_column(
        Enum(
            ExportAspectRatio,
            name="export_aspect_ratio",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=_enum_values,
        )
    )
    reframe_mode: Mapped[ReframeMode] = mapped_column(
        Enum(
            ReframeMode,
            name="export_reframe_mode",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=_enum_values,
        ),
        default=ReframeMode.CENTER,
        server_default=ReframeMode.CENTER.value,
    )
    include_captions: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[ExportStatus] = mapped_column(
        Enum(
            ExportStatus,
            name="export_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=_enum_values,
        ),
        default=ExportStatus.QUEUED,
        server_default=ExportStatus.QUEUED.value,
    )
    # Set only once the rendered file has been validated and stored.
    storage_key: Mapped[str | None] = mapped_column(String(STORAGE_KEY_MAX_LENGTH))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    # A short public message (see app.worker.jobs.sanitize_error_message);
    # never tool output, paths or stack traces.
    error_message: Mapped[str | None] = mapped_column(Text)
    # The render job (processing_jobs.id) that owns this export. Indexed for
    # the stale-job recovery in app.worker.jobs, which looks exports up by job.
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        status = (
            self.status.value if isinstance(self.status, ExportStatus) else self.status
        )
        return f"Export(id={self.id!r}, status={status!r})"
