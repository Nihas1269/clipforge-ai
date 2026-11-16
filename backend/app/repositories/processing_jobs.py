import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.processing_job import ProcessingJob


def get_job_for_user(
    db: Session, job_id: uuid.UUID, user_id: uuid.UUID
) -> ProcessingJob | None:
    """Return the job if it exists and belongs to `user_id`, else None.

    Jobs of other users and system jobs without an owner are never returned.
    """
    return db.scalars(
        select(ProcessingJob).where(
            ProcessingJob.id == job_id, ProcessingJob.user_id == user_id
        )
    ).one_or_none()
