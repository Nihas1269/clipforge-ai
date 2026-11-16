import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

IDEMPOTENCY_KEY_MAX_LENGTH = 255
# "<job_type>:<idempotency_key>"
ACTIVE_KEY_MAX_LENGTH = 320
CELERY_TASK_ID_MAX_LENGTH = 255
REQUEST_ID_MAX_LENGTH = 64


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobType(str, enum.Enum):
    """Kind of background processing a job performs."""

    METADATA = "metadata"
    TRANSCRIPTION = "transcription"
    CLIP_DETECTION = "clip_detection"
    RENDER = "render"


class JobStatus(str, enum.Enum):
    """Lifecycle of a processing job.

    queued -> running -> succeeded | failed; a retryable failure goes back to
    queued. Queued and running jobs can be cancelled.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_JOB_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})


def _enum_values(members: type[enum.Enum]) -> list[str]:
    return [member.value for member in members]


class ProcessingJob(Base):
    """A persistent record of one background processing job.

    Change the status only through app.worker.jobs, which uses conditional
    UPDATE statements so concurrent workers cannot both claim or finish a job.
    """

    __tablename__ = "processing_jobs"
    __table_args__ = (
        CheckConstraint(
            "attempts >= 0", name="ck_processing_jobs_attempts_non_negative"
        ),
        CheckConstraint(
            "max_attempts >= 1", name="ck_processing_jobs_max_attempts_positive"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    video_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("videos.id", ondelete="CASCADE"), index=True
    )
    job_type: Mapped[JobType] = mapped_column(
        Enum(
            JobType,
            name="processing_job_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=_enum_values,
        )
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(
            JobStatus,
            name="processing_job_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=_enum_values,
        ),
        default=JobStatus.QUEUED,
        server_default=JobStatus.QUEUED.value,
        index=True,
    )
    # Identifies the operation, for example the video id for a metadata job.
    idempotency_key: Mapped[str | None] = mapped_column(
        String(IDEMPOTENCY_KEY_MAX_LENGTH)
    )
    # "<job_type>:<idempotency_key>" while the job is queued or running, NULL
    # once it has finished. The unique index allows only one active job per
    # operation.
    active_key: Mapped[str | None] = mapped_column(
        String(ACTIVE_KEY_MAX_LENGTH), unique=True, index=True
    )
    celery_task_id: Mapped[str | None] = mapped_column(
        String(CELERY_TASK_ID_MAX_LENGTH)
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1")
    )
    # Sanitized and truncated; never a stack trace or raw exception text.
    error_message: Mapped[str | None] = mapped_column(Text)
    # none_as_null stores a missing payload as SQL NULL, not JSON "null".
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    # Request ID of the API request that queued the job, for log correlation.
    request_id: Mapped[str | None] = mapped_column(String(REQUEST_ID_MAX_LENGTH))
    # When the job was queued.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    # When the latest attempt started.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        # No payload: it can contain user content.
        status = (
            self.status.value if isinstance(self.status, JobStatus) else self.status
        )
        return f"ProcessingJob(id={self.id!r}, status={status!r})"
