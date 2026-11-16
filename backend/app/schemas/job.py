import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.processing_job import JobStatus, JobType


class JobRead(BaseModel):
    """A processing job as returned to its owner.

    The payload, Celery task id, idempotency key and request id are internal
    and are deliberately not exposed. `error_message` is already sanitized.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_type: JobType
    status: JobStatus
    video_id: uuid.UUID | None = None
    attempts: int
    max_attempts: int
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
