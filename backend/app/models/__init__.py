"""Database models.

Import every model here so that `Base.metadata` is complete for Alembic.
"""

from app.models.auth_token import AuthToken, AuthTokenPurpose
from app.models.clip import Clip, ClipSource, ClipStatus
from app.models.processing_job import JobStatus, JobType, ProcessingJob
from app.models.storage_deletion import PendingStorageDeletion
from app.models.transcript import Transcript, TranscriptSegment
from app.models.user import User
from app.models.video import Video

__all__ = [
    "AuthToken",
    "AuthTokenPurpose",
    "Clip",
    "ClipSource",
    "ClipStatus",
    "JobStatus",
    "JobType",
    "PendingStorageDeletion",
    "ProcessingJob",
    "Transcript",
    "TranscriptSegment",
    "User",
    "Video",
]
