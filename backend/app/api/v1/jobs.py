"""Status of background processing jobs.

Only the job's owner can read it; another user's job and jobs without an
owner get the same 404 as a job that does not exist.
"""

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import AppError
from app.database import get_db
from app.models.user import User
from app.repositories.processing_jobs import get_job_for_user
from app.schemas.job import JobRead

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobRead)
def read_job(
    job_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobRead:
    job = get_job_for_user(db, job_id, user.id)
    if job is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Job not found.")
    return JobRead.model_validate(job)
